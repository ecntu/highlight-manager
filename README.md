# Personal Highlight Manager (PHM)

A personal highlight manager for capturing and organizing highlights across multiple media sources.

## Setup

```bash
# Install dependencies
uv sync

# Create database
createdb highlight_manager

# Configure environment
cp .env.example .env
# Edit .env with your database credentials

# Initialize database
uv run python init_db.py

# Run the server
uv run uvicorn app.main:app --reload
```

Visit http://localhost:8000

## Browser Extension

A minimal MV3 browser extension lives in [`browser-extension/`](./browser-extension).

It supports:
- saving selected text from the current web page
- optional tags
- optional first note
- optional reminder preset on create

To load it in Chrome:

```bash
open chrome://extensions
```

Then enable Developer Mode, choose `Load unpacked`, and select `browser-extension/`.

To load it in Firefox:

```bash
open about:debugging#/runtime/this-firefox
```

Then choose `Load Temporary Add-on` and select `browser-extension/manifest.json`.

In the extension settings, provide:
- PHM base URL, for example `http://localhost:8000`
- an add-only device API key from Settings

## TODOs
- [ ] resurfacing features (random, digests)
- [ ] semantic search
- [ ] auto fix spelling / formatting mistakes
- [ ] android clipper
