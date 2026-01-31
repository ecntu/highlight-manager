# Project Spec: Personal Highlight Manager (PHM)

## 0) One-liner

A personal highlight manager that captures highlights across media, preserves source context, supports tagging/linking, and resurfaces highlights. Data can be added from multiple devices using per-device API keys.

## 1) Goals

* Store highlights from multiple media in one place
* Preserve source context (source metadata + location + capture time)
* Multi-device capture via simple API calls (device API key auth)
* Retrieval via full-text search (semantic optional later)
* Lightweight linking + collections
* Resurfacing via daily/weekly digests

## 2) Non-goals (v1)

* Task management / journaling / social
* In-app PDF/EPUB annotation (import extracted highlights is enough)
* Complex conflict resolution/version history
* Hard deletes via API (write/read only)

---

## 3) Architecture

### Backend (primary)

* **FastAPI** + Pydantic
* DB: **Postgres**
* ORM: SQLAlchemy 2.0 (or SQLModel) + Alembic migrations
* Auth:

  * **Web UI login** (minimal) for user + device management (session/JWT)
  * **Device API keys** for automated capture + read access

### Frontend (optional but recommended)

* Minimal web UI (Jinja templates + HTMX when needed).
* The backend is the source of truth either way.

---

## 4) Auth Model: Tiny Users + Device API Keys

### Users

* A “user” is the owner of a highlight library.
* Users log into the web UI to:

  * view/edit highlights
  * create/revoke device keys
  * configure digests
  * import/export data

### Devices

* Each device has its own API key.
* Keys are **bearer tokens** used in:

  * `Authorization: Bearer <DEVICE_API_KEY>`
* Keys allow:

  * ✅ Create highlights (write)
  * ✅ Read highlights/sources/tags (read)
  * ❌ Delete highlights/sources/tags (no delete endpoints for device keys)
* Keys can be revoked from the UI.

### Key handling requirements

* Store only a **hash** of the API key (never store the raw key)
* Key format: `phm_live_<random>` (random 32+ bytes)
* Track: `device_name`, `created_at`, `last_used_at`, `revoked_at`
* Basic per-key rate limiting (simple fixed window is fine)

---

## 5) Core Use Cases

### Capture (multi-device)

1. Device posts highlight text + optional metadata to an ingest endpoint.
2. Minimal input should work: only `text` is required.

### Organize

* Tag highlights
* Add note (“why it matters”)
* Link highlights
* Add to collections

### Retrieve

* Full-text search across highlight `text` + `note` + source metadata
* Filter by tags/source type/date/collection

### Resurface

* Daily resurfacing endpoint: “N highlights”
* Weekly digest endpoint: summary stats + selections

---

## 6) UX Requirements (v1 screens)

1. Home: quick add, today’s resurfaced, recent highlights
2. Highlight detail: edit text/note/tags, source info, links
3. Sources: list + source detail
4. Search: query + filters
5. Collections: create + manage membership
6. Import/Export
7. Settings: digest config + device key management (create/revoke)

---

## 7) Data Model (Postgres)

### Enums

* `source_type`: `book | web`
* `link_type`: `related | supports | contradicts | example | expands`
* `highlight_status`: `active | archived`
* `auth_type`: `ui_session | device_key`

### Tables

#### `users`

* `id` uuid pk
* `email` (unique) (or username)
* `password_hash` (if not using magic link)
* `created_at`

#### `devices`

* `id` uuid pk
* `user_id` fk
* `name` text
* `api_key_hash` text (unique)
* `prefix` text (e.g. `phm_live_...` prefix for identification)
* `created_at`
* `last_used_at`
* `revoked_at` nullable

Indexes: `(user_id)`, `(api_key_hash)`

#### `sources`

* `id` uuid pk
* `user_id` fk
* `domain` text nullable (only for web - e.g. "nytimes.com")
* `title` text nullable (only for books)
* `author` text nullable (only for books)
* `type` source_type required (book | web)
* `created_at`
* `updated_at`

**Design:**
* **Web sources:** domain-level only (e.g., "nytimes.com"). Individual article URLs stored on highlights.
* **Book sources:** title + author. Each book is one source.

Matching logic:
* Web: match by domain (case-insensitive)
* Book: match by title (case-insensitive)

Indexes: `(user_id, domain)`, `(user_id, title)`

#### `highlights`

* `id` uuid pk
* `user_id` fk
* `source_id` fk nullable
* `device_id` fk nullable (if created via device key)
* `text` text required
* `note` text nullable
* `url` text nullable (for web highlights - full article URL)
* `page_title` text nullable (for web highlights - article title)
* `page_author` text nullable (for web highlights - article author)
* `location` jsonb nullable `{page, chapter}` (for books only)
* `status` highlight_status default active
* `is_favorite` bool default false
* `created_at`
* `updated_at`
* `highlighted_at` nullable (original time if known)

Indexes: `(user_id, created_at desc)`, `(user_id, source_id)`, `(user_id, is_favorite)`, `(user_id, device_id)`
Search: Postgres FTS index on `(text, note)` via `tsvector`

#### `tags`

* `id` uuid pk
* `user_id` fk
* `name` citext unique per user
* `created_at`

#### `highlight_tags`

* `highlight_id` fk
* `tag_id` fk
  PK `(highlight_id, tag_id)`

#### `collections`

* `id` uuid pk
* `user_id` fk
* `name` text
* `description` text nullable
* `created_at`

#### `collection_items`

* `collection_id` fk
* `highlight_id` fk
* `added_at`
  PK `(collection_id, highlight_id)`

#### `highlight_links`

* `id` uuid pk
* `user_id` fk
* `from_highlight_id` fk
* `to_highlight_id` fk
* `type` link_type
* `note` text nullable
* `created_at`
  Constraints: no self-link, unique `(from_highlight_id, to_highlight_id, type)`

#### `digest_config`

* `user_id` pk/fk
* `daily_count` int default 5
* `tag_focus` text[] default empty
* `timezone` text default `"America/Detroit"`
* `created_at`
* `updated_at`

---

## 8) API Spec (FastAPI REST)

### Authentication

#### Device key auth (for devices/scripts)

* Header: `Authorization: Bearer <device_api_key>`
* Device keys can access:

  * ingest highlight
  * read endpoints
  * update endpoints only if you want (optional; can restrict to UI only)

#### UI auth (for web app)

* Session cookie or JWT
* Full access (including delete if you later add it—v1 can avoid deletes entirely)

---

## RESTful API Endpoints

### Auth

* `POST /register` - create user account
* `POST /login` - authenticate user (session-based)
* `GET /logout` - end session

### Highlights

* `GET /highlights` - list highlights with filters (`?q=search&tag=name&source_id=uuid&status=active&favorite=true`)
* `POST /highlights` - create new highlight (auth: UI session or device key)
* `GET /highlights/{id}` - get highlight detail
* `PATCH /highlights/{id}` - update highlight (text, note, tags, source) *(UI only)*
* `DELETE /highlights/{id}` - archive highlight *(UI only)*
* `PUT /highlights/{id}/favorite` - toggle favorite status *(UI only)*

**Device API** (same endpoint, device key auth):
* `POST /api/highlights` - simplified ingest
  * Form fields: `text` (required), `note`, `tags` (csv), `source_url`, `source_title`, `source_author`, `location` (json)
  * Source matching: if `source_url` provided, match by URL; otherwise match by `source_title`

### Sources

* `GET /sources` - list all sources (`?type=book&q=search`)
* `GET /sources/{id}` - get source detail with highlights

### Tags

* `GET /tags` - list all tags (`?q=search`)

### Collections

* `GET /collections` - list collections
* `POST /collections` - create collection
* `GET /collections/{id}` - get collection detail
* `PATCH /collections/{id}` - update collection
* `DELETE /collections/{id}` - delete collection
* `PUT /collections/{id}/highlights/{highlight_id}` - add highlight to collection
* `DELETE /collections/{id}/highlights/{highlight_id}` - remove highlight from collection

### Links

* `GET /links` - list highlight links
* `POST /links` - create link between highlights
* `DELETE /links/{id}` - delete link

### Devices (UI only)

* `GET /devices` - list active devices
* `POST /devices` - create device API key (returns raw key once)
* `DELETE /devices/{id}` - revoke device

### Digest

* `GET /digest/today` - daily highlights
* `GET /digest/weekly` - weekly summary (`?week=YYYY-WW`)

### Import/Export (UI only)

* `POST /import/kindle` - import Kindle highlights
* `POST /import/csv` - import CSV file
* `GET /export/json` - export all data as JSON
* `GET /export/csv` - export highlights as CSV

### Settings (UI only)

* `GET /settings` - view settings page
* `PATCH /digest-config` - update digest preferences

---


Same as before (simple scoring based on time since last reviewed, favorites, tag focus, link degree). Track:

* `highlights.last_reviewed_at` (add this field if you want; optional)
* `highlights.review_count` (optional)

Endpoint `POST /api/highlights/{id}/review` updates those.

---

## 10) Acceptance Criteria

* Can add highlights from multiple devices using device API keys
* Keys are revocable; server stores only hashes
* Device keys can **create + read**, but cannot delete
* UI can view/edit/tag/link/search highlights
* Import Kindle + CSV works
* Daily digest endpoint returns N highlights

---

## 11) Milestones

1. FastAPI + Postgres + migrations + basic UI auth
2. Device keys: create/list/revoke + ingest endpoint
3. Highlights CRUD + tags + sources + FTS search
4. Links + collections
5. Import/export
6. Digests