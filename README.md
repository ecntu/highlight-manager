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

## TODOs
- [ ] test MoonReader sync
- [ ] allow for editing highlights and source names while avoiding duplicates on re-import
- [ ] resurfacing features (reminders, random, digests)
- [ ] read-only API keys to export / mcp
