"""
Markdown to PDF — production FastAPI app.

Environment variables (see .env.example):
    APP_NAME           Display name (default: "Markdown to PDF")
    APP_URL            Canonical URL, used in og/meta tags
    APP_TAGLINE        One-liner shown on landing/about
    DONATE_URL         Link shown in the "Support" CTA
    CONTACT_EMAIL      Shown in the about/privacy modal
    PRO_KEYS           Comma-separated unlock keys for pro tier
    MAX_MD_BYTES_FREE  Max markdown size for anon users (default 50000)
    MAX_MD_BYTES_PRO   Max markdown size for pro users (default 1000000)
    RATE_LIMIT_FREE    Conversions per hour, anon (default 10)
    RATE_LIMIT_PRO     Conversions per hour, pro (default 200)
    WATERMARK_FREE     "true"/"false" — add footer watermark to free-tier PDFs
    CORS_ORIGINS       Comma-separated allowed origins, or "*"
"""
from __future__ import annotations

import io
import logging
import os
import re
import time
from collections import defaultdict, deque
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
import markdown
import yaml
from pygments.formatters import HtmlFormatter
from xhtml2pdf import pisa

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, ""))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


APP_NAME = _env("APP_NAME", "Markdown to PDF")
APP_URL = _env("APP_URL", "http://localhost:8000")
APP_TAGLINE = _env(
    "APP_TAGLINE",
    "Free, private, browser-based Markdown → PDF converter. No sign-up, no tracking.",
)
DONATE_URL = _env("DONATE_URL", "https://www.buymeacoffee.com/")
CONTACT_EMAIL = _env("CONTACT_EMAIL", "")

PRO_KEYS = {k.strip() for k in _env("PRO_KEYS", "").split(",") if k.strip()}
MAX_MD_BYTES_FREE = _env_int("MAX_MD_BYTES_FREE", 50_000)
MAX_MD_BYTES_PRO = _env_int("MAX_MD_BYTES_PRO", 1_000_000)
RATE_LIMIT_FREE = _env_int("RATE_LIMIT_FREE", 10)
RATE_LIMIT_PRO = _env_int("RATE_LIMIT_PRO", 200)
WATERMARK_FREE = _env_bool("WATERMARK_FREE", True)

CORS_RAW = _env("CORS_ORIGINS", "*")
CORS_ORIGINS = ["*"] if CORS_RAW.strip() == "*" else [o.strip() for o in CORS_RAW.split(",") if o.strip()]

MAX_UPLOAD_BYTES = 2_000_000  # 2 MB hard cap on file uploads
RATE_WINDOW_SEC = 3600

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("mdtopdf")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory="templates")

app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    return response


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

MD_EXTENSIONS = [
    "extra",
    "tables",
    "fenced_code",
    "codehilite",
    "sane_lists",
    "smarty",
    "nl2br",
    "toc",
]
MD_EXT_CONFIG = {
    "codehilite": {"css_class": "codehilite", "guess_lang": False},
    "toc": {"title": "Table of Contents", "toc_depth": "2-4"},
}

PYGMENTS_CSS = HtmlFormatter(style="friendly").get_style_defs(".codehilite")

PAGE_DIMENSIONS = {
    "A4":     {"w": 595, "h": 842, "css": "a4"},
    "Letter": {"w": 612, "h": 792, "css": "letter"},
}

FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._\- ]")

THEMES = {
    "github": """
body { font-family: Helvetica, Arial, sans-serif; line-height: 1.55; color: #24292f; }
h1, h2, h3, h4 { color: #1f2328; }
h1 { border-bottom: 2px solid #d0d7de; padding-bottom: 6px; }
h2 { border-bottom: 1px solid #d0d7de; padding-bottom: 4px; }
a { color: #0969da; }
code { background: #eaeef2; color: #1f2328; }
.codehilite { background: #f6f8fa; border: 1px solid #d0d7de; }
blockquote { border-left: 4px solid #d0d7de; color: #57606a; background: #f6f8fa; }
th { background-color: #f6f8fa; }
""",
    "academic": """
body { font-family: 'Times New Roman', Times, serif; line-height: 1.7; color: #000; font-size: 12pt; }
h1, h2, h3, h4 { font-family: 'Times New Roman', serif; font-weight: bold; color: #000; }
h1 { text-align: center; font-size: 18pt; margin-top: 0; }
h2 { font-size: 14pt; }
h3 { font-size: 12pt; font-style: italic; }
p { text-align: justify; text-indent: 1.5em; margin: 0.3em 0; }
code { background: #f0f0f0; color: #000; font-family: 'Courier New', monospace; }
.codehilite { background: #f8f8f8; border: 1px solid #ccc; }
blockquote { border-left: 3px solid #666; color: #333; font-style: italic; }
""",
    "minimal": """
body { font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; line-height: 1.8; color: #333; font-weight: 300; }
h1, h2, h3, h4 { font-weight: 400; color: #111; letter-spacing: -0.01em; }
h1 { font-size: 28pt; margin-bottom: 0.5em; }
h2 { font-size: 18pt; color: #444; }
a { color: #555; border-bottom: 1px solid #aaa; text-decoration: none; }
code { background: #f5f5f5; color: #555; }
.codehilite { background: #fafafa; border: none; }
blockquote { border-left: 2px solid #ccc; color: #666; font-style: italic; background: transparent; }
th { background-color: #fafafa; color: #666; font-weight: 400; }
""",
    "dark": """
body { font-family: Helvetica, Arial, sans-serif; line-height: 1.6; color: #e6edf3; background: #0d1117; }
h1, h2, h3, h4 { color: #f0f6fc; }
h1 { border-bottom: 2px solid #30363d; padding-bottom: 6px; }
h2 { border-bottom: 1px solid #30363d; padding-bottom: 4px; }
a { color: #58a6ff; }
code { background: #161b22; color: #c9d1d9; }
.codehilite { background: #161b22; border: 1px solid #30363d; }
blockquote { border-left: 4px solid #30363d; color: #8b949e; background: #161b22; }
th { background-color: #161b22; color: #f0f6fc; }
td { border-color: #30363d; }
""",
}

BASE_CSS = """
h1, h2, h3, h4 { margin-top: 1.4em; margin-bottom: 0.5em; }
p { margin: 0.6em 0; }
a { text-decoration: none; }
code { padding: 2px 5px; border-radius: 3px; font-family: Courier, monospace; font-size: 0.92em; }
pre { padding: 12px; border-radius: 6px; font-family: Courier, monospace; font-size: 0.88em; }
pre code { background: none; padding: 0; }
.codehilite { padding: 12px; border-radius: 6px; margin: 1em 0; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; }
th, td { border: 1px solid #d1d5db; padding: 8px; text-align: left; }
blockquote { margin: 0.8em 0; padding: 4px 15px; }
img { max-width: 100%; }
hr { border: none; border-top: 1px solid #e2e8f0; margin: 1.5em 0; }
.toc { background: rgba(0,0,0,0.03); padding: 16px 24px; border-radius: 6px; margin: 1em 0; }
.toc > .toctitle { font-weight: bold; font-size: 1.1em; margin-bottom: 8px; }
.toc ul { margin: 0 0 0 16px; }

.cover-page { text-align: center; padding-top: 180pt; page-break-after: always; }
.cover-title { font-size: 32pt; margin-bottom: 12pt; border: none; }
.cover-subtitle { font-size: 18pt; font-weight: normal; margin-bottom: 60pt; border: none; color: #555; }
.cover-author { font-size: 14pt; margin-top: 40pt; }
.cover-date { font-size: 12pt; color: #666; margin-top: 8pt; }
"""


def parse_front_matter(text: str) -> tuple[dict, str]:
    m = FRONT_MATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        data = yaml.safe_load(m.group(1)) or {}
        if not isinstance(data, dict):
            return {}, text
    except yaml.YAMLError:
        return {}, text
    return data, text[m.end():]


def build_page_css(page_size: str, for_pdf: bool) -> str:
    dims = PAGE_DIMENSIONS.get(page_size, PAGE_DIMENSIONS["A4"])
    if not for_pdf:
        return f"@page {{ size: {dims['css']}; margin: 2cm; }}"
    w, h = dims["w"], dims["h"]
    margin = 50
    footer_h = 25
    content_h = h - (2 * margin) - footer_h
    footer_top = h - margin - footer_h
    return f"""
@page {{
    size: {dims['css']};
    margin: 0;
    @frame content_frame {{
        left: {margin}pt; width: {w - 2*margin}pt;
        top: {margin}pt; height: {content_h}pt;
    }}
    @frame footer_frame {{
        -pdf-frame-content: footerContent;
        left: {margin}pt; width: {w - 2*margin}pt;
        top: {footer_top}pt; height: {footer_h}pt;
    }}
}}
"""


def build_cover_html(meta: dict) -> str:
    if not meta.get("title"):
        return ""
    parts = [f'<h1 class="cover-title">{meta["title"]}</h1>']
    if meta.get("subtitle"):
        parts.append(f'<h2 class="cover-subtitle">{meta["subtitle"]}</h2>')
    if meta.get("author"):
        parts.append(f'<p class="cover-author">{meta["author"]}</p>')
    if meta.get("date"):
        parts.append(f'<p class="cover-date">{meta["date"]}</p>')
    return f'<div class="cover-page">{"".join(parts)}</div>'


def render_html(
    md_text: str,
    page_size: str = "A4",
    theme: str = "github",
    for_pdf: bool = False,
    watermark: bool = False,
) -> str:
    meta, body_text = parse_front_matter(md_text)

    if meta.get("toc") and "[TOC]" not in body_text and "[toc]" not in body_text:
        body_text = "[TOC]\n\n" + body_text

    html_content = markdown.markdown(
        body_text, extensions=MD_EXTENSIONS, extension_configs=MD_EXT_CONFIG
    )

    cover_html = build_cover_html(meta) if for_pdf else ""
    page_css = build_page_css(page_size, for_pdf=for_pdf)
    theme_css = THEMES.get(theme, THEMES["github"])

    footer_parts = []
    if meta.get("footer"):
        footer_parts.append(str(meta["footer"]))
    elif meta.get("title"):
        footer_parts.append(str(meta["title"]))
    if watermark:
        footer_parts.append(f"Created with {APP_NAME}")
    footer_parts.append("Page <pdf:pagenumber> of <pdf:pagecount>")
    footer_text = " &nbsp;&middot;&nbsp; ".join(footer_parts)

    footer_html = (
        f'<div id="footerContent" style="font-size: 9pt; color: #888; text-align: center;">'
        f"{footer_text}</div>"
        if for_pdf
        else ""
    )

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
    {page_css}
    {BASE_CSS}
    {theme_css}
    {PYGMENTS_CSS}
    </style>
</head>
<body>
{cover_html}
{html_content}
{footer_html}
</body>
</html>"""


def safe_filename(name: str) -> str:
    cleaned = FILENAME_SAFE_RE.sub("_", name).strip() or "document"
    return cleaned if cleaned.lower().endswith(".pdf") else f"{cleaned}.pdf"


# ---------------------------------------------------------------------------
# Rate limiting (in-memory sliding window per IP)
# ---------------------------------------------------------------------------

_rate_windows: dict[str, deque] = defaultdict(deque)


def client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "anon"


def check_rate_limit(key: str, limit: int) -> tuple[bool, int]:
    now = time.time()
    window = _rate_windows[key]
    cutoff = now - RATE_WINDOW_SEC
    while window and window[0] < cutoff:
        window.popleft()
    if len(window) >= limit:
        return False, 0
    window.append(now)
    return True, limit - len(window)


def is_pro(request: Request, body_key: Optional[str] = None) -> bool:
    if not PRO_KEYS:
        return False
    header = request.headers.get("x-pro-key", "").strip()
    if header and header in PRO_KEYS:
        return True
    if body_key and body_key.strip() in PRO_KEYS:
        return True
    return False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "app_name": APP_NAME,
            "app_url": APP_URL,
            "app_tagline": APP_TAGLINE,
            "donate_url": DONATE_URL,
            "contact_email": CONTACT_EMAIL,
            "pro_enabled": bool(PRO_KEYS),
        },
    )


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "app": APP_NAME}


@app.get("/api/config")
async def api_config(request: Request):
    pro = is_pro(request)
    return {
        "app_name": APP_NAME,
        "tagline": APP_TAGLINE,
        "donate_url": DONATE_URL,
        "pro_enabled": bool(PRO_KEYS),
        "is_pro": pro,
        "limits": {
            "max_md_bytes": MAX_MD_BYTES_PRO if pro else MAX_MD_BYTES_FREE,
            "rate_limit_per_hour": RATE_LIMIT_PRO if pro else RATE_LIMIT_FREE,
        },
        "watermark": WATERMARK_FREE and not pro,
    }


@app.get("/robots.txt", response_class=Response)
async def robots():
    body = f"User-agent: *\nAllow: /\nSitemap: {APP_URL}/sitemap.xml\n"
    return Response(content=body, media_type="text/plain")


@app.get("/sitemap.xml", response_class=Response)
async def sitemap():
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"<url><loc>{APP_URL}/</loc></url>\n"
        "</urlset>\n"
    )
    return Response(content=body, media_type="application/xml")


@app.post("/preview", response_class=HTMLResponse)
async def preview(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    md_text = data.get("markdown", "") or ""
    theme = data.get("theme", "github")

    pro = is_pro(request, data.get("pro_key"))
    max_bytes = MAX_MD_BYTES_PRO if pro else MAX_MD_BYTES_FREE
    if len(md_text.encode("utf-8")) > max_bytes:
        return HTMLResponse(
            f"<p style='padding:20px;color:#dc2626'>Content exceeds {max_bytes // 1000} KB limit.</p>"
        )

    if not md_text.strip():
        return HTMLResponse("<p style='padding:20px;color:#64748b'>Nothing to preview yet.</p>")
    return HTMLResponse(render_html(md_text, theme=theme, for_pdf=False))


@app.post("/convert")
async def convert_markdown(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    md_text: str = data.get("markdown", "") or ""
    filename: str = (data.get("filename") or "document").strip()
    page_size: str = data.get("page_size", "A4")
    theme: str = data.get("theme", "github")
    body_pro_key: Optional[str] = data.get("pro_key")

    if not md_text.strip():
        return JSONResponse({"error": "Please provide Markdown content"}, status_code=400)

    pro = is_pro(request, body_pro_key)
    max_bytes = MAX_MD_BYTES_PRO if pro else MAX_MD_BYTES_FREE
    if len(md_text.encode("utf-8")) > max_bytes:
        return JSONResponse(
            {
                "error": f"Content exceeds {max_bytes // 1000} KB limit. Upgrade for larger files.",
                "code": "size_limit",
                "limit_bytes": max_bytes,
            },
            status_code=413,
        )

    limit = RATE_LIMIT_PRO if pro else RATE_LIMIT_FREE
    ok, remaining = check_rate_limit(client_key(request), limit)
    if not ok:
        return JSONResponse(
            {
                "error": "Hourly limit reached. Please wait or upgrade for higher limits.",
                "code": "rate_limit",
                "limit_per_hour": limit,
            },
            status_code=429,
        )

    watermark = WATERMARK_FREE and not pro
    styled_html = render_html(
        md_text, page_size=page_size, theme=theme, for_pdf=True, watermark=watermark
    )

    pdf_buffer = io.BytesIO()
    try:
        result = pisa.CreatePDF(src=styled_html, dest=pdf_buffer, encoding="utf-8")
    except Exception as e:
        log.exception("PDF generation crashed: %s", e)
        return JSONResponse({"error": "PDF generation crashed"}, status_code=500)

    if result.err:
        log.warning("pisa reported errors generating PDF")
        return JSONResponse({"error": "Failed to generate PDF"}, status_code=500)

    pdf_buffer.seek(0)
    headers = {
        "Content-Disposition": f'inline; filename="{safe_filename(filename)}"',
        "X-RateLimit-Remaining": str(remaining),
        "X-Pro": "1" if pro else "0",
    }
    return Response(content=pdf_buffer.read(), media_type="application/pdf", headers=headers)


@app.post("/upload")
async def upload_markdown(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith((".md", ".markdown", ".txt")):
        return JSONResponse({"error": "Please upload a .md, .markdown, or .txt file"}, status_code=400)
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        return JSONResponse(
            {"error": f"File exceeds {MAX_UPLOAD_BYTES // 1000} KB hard limit"},
            status_code=413,
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")
    stem = file.filename.rsplit(".", 1)[0]
    return JSONResponse({"filename": stem, "markdown": text})


@app.post("/api/verify-key")
async def verify_key(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"valid": False}, status_code=400)
    key = (data.get("key") or "").strip()
    return {"valid": bool(key and key in PRO_KEYS)}


@app.on_event("startup")
async def on_startup():
    log.info(
        "%s started — pro_enabled=%s, free_limit=%s/h, free_max_bytes=%s",
        APP_NAME,
        bool(PRO_KEYS),
        RATE_LIMIT_FREE,
        MAX_MD_BYTES_FREE,
    )
