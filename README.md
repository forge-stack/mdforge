# Markdown to PDF

A fast, private, browser-based Markdown → PDF converter. No sign-up, nothing stored, works offline-ish (the editor autosaves to your browser).

I built this because every other tool I tried wanted an account, pushed ads, or mangled my code blocks. This one doesn't.

## What it does

- **Live HTML preview** as you type (debounced, 300ms)
- **Four themes** — GitHub, Academic, Minimal, Dark
- **YAML front matter** → auto cover page, author, date, footer
- **Auto table of contents** when you set `toc: true`
- **Page numbers** in the footer
- **Syntax-highlighted** code blocks (Pygments, friendly style)
- **Starter templates** — Resume, Business Report, Formal Letter, Academic Paper
- **Drag-and-drop** `.md` files anywhere in the editor
- **Autosave** to localStorage, so a refresh never loses your work
- **Ctrl+S** generates a PDF. That's it.

## Front matter example

Start your document with a YAML block and the app generates a cover page + TOC:

```markdown
---
title: Q1 2026 Performance Report
subtitle: Revenue, growth, and key initiatives
author: Jane Doe
date: April 15, 2026
toc: true
footer: Confidential — internal use only
---

## Executive Summary
...
```

All fields are optional. If there's no `title`, you get no cover page — just the document.

## Running it locally

You need Python 3.12+. The project uses `uv` but plain `pip` works too.

```bash
# With uv
uv sync
uv run uvicorn main:app --reload

# Or plain pip
python -m venv .venv
.venv/Scripts/activate        # Windows
# source .venv/bin/activate   # macOS / Linux
pip install -r requirements.txt
uvicorn main:app --reload
```

Open `http://127.0.0.1:8000/` and start typing.

## Configuration

Everything is environment-driven. Copy `.env.example` → `.env` and fill in what you need:

| Variable | What it does | Default |
|---|---|---|
| `APP_NAME` | Name in the header and meta tags | `Markdown to PDF` |
| `APP_URL` | Canonical URL for SEO / sitemap | `http://localhost:8000` |
| `APP_TAGLINE` | One-liner for meta description | see `.env.example` |
| `DONATE_URL` | Link behind the "Buy me a coffee" button | Buy Me A Coffee |
| `CONTACT_EMAIL` | Shown in the About modal (optional) | empty |
| `PRO_KEYS` | Comma-separated license keys for the Pro tier | empty (Pro disabled) |
| `MAX_MD_BYTES_FREE` | Markdown size cap for anonymous users | `50000` |
| `MAX_MD_BYTES_PRO` | Markdown size cap for Pro users | `1000000` |
| `RATE_LIMIT_FREE` | Conversions per hour, free tier | `10` |
| `RATE_LIMIT_PRO` | Conversions per hour, Pro tier | `200` |
| `WATERMARK_FREE` | Add a "Created with…" footer line on free PDFs | `true` |
| `CORS_ORIGINS` | Comma-separated allowed origins, or `*` | `*` |

If `PRO_KEYS` is empty, the Pro upgrade UI disappears entirely — useful if you just want to run this as a free internal tool.

## Deploying

There's a production Dockerfile in the repo.

```bash
docker build -t mdtopdf .
docker run -p 8000:8000 --env-file .env mdtopdf
```

It runs as a non-root user, has a healthcheck on `/healthz`, and starts Uvicorn with 2 workers. Should drop into Fly.io, Railway, Render, or any VPS without changes.

### Reverse proxy note

Rate limiting uses the client IP. If you're behind Nginx/Cloudflare/Fly, the app reads `X-Forwarded-For` automatically — just make sure your proxy is setting it.

## Monetization (if you're launching it)

This is set up so you can sell it as a freemium tool without extra infrastructure:

1. Generate some license keys: `python -c "import secrets; [print(secrets.token_hex(16)) for _ in range(50)]"`
2. Put them in `PRO_KEYS` as a comma-separated list.
3. Set `DONATE_URL` to a Gumroad / Lemon Squeezy / Stripe Payment Link that sells Pro access. Deliver the key in the receipt email.
4. Free users hit the 50 KB / 10-per-hour limits → the upgrade modal opens automatically → the "Get a Pro key" button takes them to checkout.

Pro users paste their key into the upgrade modal; it's verified server-side (`/api/verify-key`) and cached in `localStorage`. No database, no accounts.

For donations only (no Pro tier), leave `PRO_KEYS` empty and the About modal's "Buy me a coffee" button becomes your sole CTA.

## API

There's a small HTTP API under the hood in case you want to script it:

| Method | Path | What it does |
|---|---|---|
| `GET` | `/` | The web UI |
| `GET` | `/healthz` | Liveness probe |
| `GET` | `/api/config` | Current user's limits / Pro status |
| `POST` | `/preview` | Markdown → HTML (live preview) |
| `POST` | `/convert` | Markdown → PDF |
| `POST` | `/upload` | `.md` file → `{ filename, markdown }` |
| `POST` | `/api/verify-key` | Validate a Pro license key |

All POST endpoints take JSON. Pass `X-Pro-Key: <key>` as a header to bypass free-tier limits.

Example:

```bash
curl -X POST http://localhost:8000/convert \
  -H "Content-Type: application/json" \
  -d '{"markdown":"# Hello","filename":"hello","theme":"github","page_size":"A4"}' \
  -o hello.pdf
```

## How it works

1. `markdown` (Python-Markdown) turns your text into HTML, with `extra`, `codehilite`, `smarty`, `nl2br`, `sane_lists`, `toc`, `tables`, `fenced_code` extensions enabled.
2. `pygments` generates the syntax highlighting CSS.
3. YAML front matter is parsed with `pyyaml` and used to build a cover page.
4. The HTML is wrapped in a theme CSS block plus xhtml2pdf `@page`/`@frame` rules that carve out space for the content and a footer with `<pdf:pagenumber>`.
5. `xhtml2pdf` (pure-Python) renders the final PDF into an in-memory buffer.

No Chrome, no wkhtmltopdf, no GTK libraries — that's the whole reason for picking xhtml2pdf. Trade-off is a smaller CSS subset (no flexbox/grid, basic font handling), which is fine for document-style output.

## Privacy

Files are never written to disk. The app parses your Markdown, renders the PDF in memory, streams it back, and forgets about it. No analytics, no third-party scripts, no cookies. The editor content you see in localStorage stays in your browser.

## License

MIT. Do whatever you want with it.

## Credits

Built on top of:

- [FastAPI](https://fastapi.tiangolo.com/) — the web framework
- [Python-Markdown](https://python-markdown.github.io/) — Markdown parsing
- [xhtml2pdf](https://xhtml2pdf.readthedocs.io/) — pure-Python PDF rendering
- [Pygments](https://pygments.org/) — syntax highlighting
- [PyYAML](https://pyyaml.org/) — front matter parsing

If you end up using this, I'd love to hear about it.
