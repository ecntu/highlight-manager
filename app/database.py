from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from app.config import settings

engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def migrate_schema():
    """Apply lightweight additive schema migrations for existing databases."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    if "sources" in tables:
        source_columns = {column["name"] for column in inspector.get_columns("sources")}
        with engine.begin() as conn:
            if "original_name" not in source_columns:
                conn.execute(text("ALTER TABLE sources ADD COLUMN original_name TEXT"))
            if "display_name" not in source_columns:
                conn.execute(text("ALTER TABLE sources ADD COLUMN display_name TEXT"))
            conn.execute(
                text(
                    "UPDATE sources "
                    "SET original_name = COALESCE(original_name, title, domain) "
                    "WHERE original_name IS NULL"
                )
            )
            conn.execute(
                text(
                    "UPDATE sources "
                    "SET display_name = COALESCE(display_name, original_name, title, domain) "
                    "WHERE display_name IS NULL"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_sources_user_original_name "
                    "ON sources (user_id, original_name)"
                )
            )

    if "devices" in tables:
        device_columns = {column["name"] for column in inspector.get_columns("devices")}
        with engine.begin() as conn:
            if "scope" not in device_columns:
                conn.execute(
                    text(
                        "ALTER TABLE devices ADD COLUMN scope TEXT DEFAULT 'add_only'"
                    )
                )
            conn.execute(
                text(
                    "UPDATE devices "
                    "SET scope = CASE "
                    "WHEN prefix = 'web' OR name = 'Web' THEN 'web' "
                    "WHEN scope IN ('add_only', 'read_only', 'web') THEN scope "
                    "ELSE 'add_only' "
                    "END"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_devices_user_scope "
                    "ON devices (user_id, scope)"
                )
            )

    if "highlights" in tables:
        highlight_columns = {
            column["name"] for column in inspector.get_columns("highlights")
        }
        with engine.begin() as conn:
            if "original_text" not in highlight_columns:
                conn.execute(text("ALTER TABLE highlights ADD COLUMN original_text TEXT"))
            if "import_fingerprint" not in highlight_columns:
                conn.execute(
                    text("ALTER TABLE highlights ADD COLUMN import_fingerprint TEXT")
                )
            conn.execute(
                text(
                    "UPDATE highlights "
                    "SET original_text = text "
                    "WHERE original_text IS NULL"
                )
            )
            conn.execute(
                text(
                    "UPDATE highlights "
                    "SET import_fingerprint = source_id || '::' || lower(trim(original_text)) "
                    "WHERE import_fingerprint IS NULL AND source_id IS NOT NULL AND original_text IS NOT NULL"
                )
            )
            conn.execute(
                text(
                    "DROP INDEX IF EXISTS ux_highlights_user_import_fingerprint"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_highlights_user_import_fingerprint "
                    "ON highlights (user_id, import_fingerprint)"
                )
            )

    if "notes" in tables and "highlights" in tables:
        note_columns = {column["name"] for column in inspector.get_columns("notes")}
        highlight_columns = {
            column["name"] for column in inspector.get_columns("highlights")
        }
        if {"user_id", "highlight_id", "source_id", "body"} <= note_columns and {
            "note",
            "created_at",
            "updated_at",
        } <= highlight_columns:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO notes (id, user_id, highlight_id, source_id, body, kind, created_at, updated_at) "
                        "SELECT lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' || substr(lower(hex(randomblob(2))), 2) || '-' "
                        "|| substr('89ab', abs(random()) % 4 + 1, 1) || substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6))), "
                        "h.user_id, h.id, NULL, h.note, 'legacy', "
                        "COALESCE(h.updated_at, h.created_at), COALESCE(h.updated_at, h.created_at) "
                        "FROM highlights h "
                        "WHERE h.note IS NOT NULL AND trim(h.note) <> '' "
                        "AND NOT EXISTS ("
                        "    SELECT 1 FROM notes n "
                        "    WHERE n.highlight_id = h.id"
                        ")"
                    )
                )

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    if "notes" in tables:
        note_columns = {column["name"] for column in inspector.get_columns("notes")}
        note_table_sql = ""
        with engine.connect() as conn:
            note_table_sql = conn.execute(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'notes'"
                )
            ).scalar() or ""
        if {"id", "user_id", "highlight_id", "source_id", "body", "created_at", "updated_at"} <= note_columns and "CHECK" not in note_table_sql:
            with engine.begin() as conn:
                conn.execute(text("PRAGMA foreign_keys=OFF"))
                conn.execute(
                    text(
                        "CREATE TABLE notes_new ("
                        "id VARCHAR(36) NOT NULL PRIMARY KEY, "
                        "user_id VARCHAR(36) NOT NULL, "
                        "highlight_id VARCHAR(36), "
                        "source_id VARCHAR(36), "
                        "body TEXT NOT NULL, "
                        "kind VARCHAR(50), "
                        "created_at DATETIME NOT NULL, "
                        "updated_at DATETIME NOT NULL, "
                        "CHECK ("
                        "    (highlight_id IS NOT NULL AND source_id IS NULL) "
                        "    OR "
                        "    (highlight_id IS NULL AND source_id IS NOT NULL)"
                        "), "
                        "FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, "
                        "FOREIGN KEY(highlight_id) REFERENCES highlights (id) ON DELETE CASCADE, "
                        "FOREIGN KEY(source_id) REFERENCES sources (id) ON DELETE CASCADE"
                        ")"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO notes_new (id, user_id, highlight_id, source_id, body, kind, created_at, updated_at) "
                        "SELECT id, user_id, highlight_id, source_id, body, kind, created_at, updated_at "
                        "FROM notes "
                        "WHERE (highlight_id IS NOT NULL AND source_id IS NULL) "
                        "   OR (highlight_id IS NULL AND source_id IS NOT NULL)"
                    )
                )
                conn.execute(text("DROP TABLE notes"))
                conn.execute(text("ALTER TABLE notes_new RENAME TO notes"))
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_notes_user_highlight "
                        "ON notes (user_id, highlight_id)"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_notes_user_source "
                        "ON notes (user_id, source_id)"
                    )
                )
                conn.execute(text("PRAGMA foreign_keys=ON"))

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    if "highlights" in tables:
        highlight_columns = {
            column["name"] for column in inspector.get_columns("highlights")
        }
        if "note" in highlight_columns or "original_note" in highlight_columns:
            with engine.begin() as conn:
                conn.execute(text("PRAGMA foreign_keys=OFF"))
                conn.execute(
                    text(
                        "CREATE TABLE highlights_new ("
                        "id VARCHAR(36) NOT NULL PRIMARY KEY, "
                        "user_id VARCHAR(36) NOT NULL, "
                        "source_id VARCHAR(36), "
                        "device_id VARCHAR(36), "
                        "text TEXT NOT NULL, "
                        "original_text TEXT, "
                        "import_fingerprint TEXT, "
                        "url TEXT, "
                        "page_title TEXT, "
                        "page_author TEXT, "
                        "location JSON, "
                        "status VARCHAR(8) NOT NULL, "
                        "is_favorite BOOLEAN NOT NULL, "
                        "created_at DATETIME NOT NULL, "
                        "updated_at DATETIME NOT NULL, "
                        "highlighted_at DATETIME, "
                        "FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, "
                        "FOREIGN KEY(source_id) REFERENCES sources (id) ON DELETE SET NULL, "
                        "FOREIGN KEY(device_id) REFERENCES devices (id) ON DELETE SET NULL"
                        ")"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO highlights_new ("
                        "id, user_id, source_id, device_id, text, original_text, import_fingerprint, "
                        "url, page_title, page_author, location, status, is_favorite, created_at, updated_at, highlighted_at"
                        ") "
                        "SELECT "
                        "id, user_id, source_id, device_id, text, original_text, import_fingerprint, "
                        "url, page_title, page_author, location, status, is_favorite, created_at, updated_at, highlighted_at "
                        "FROM highlights"
                    )
                )
                conn.execute(text("DROP TABLE highlights"))
                conn.execute(text("ALTER TABLE highlights_new RENAME TO highlights"))
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_highlights_user_import_fingerprint "
                        "ON highlights (user_id, import_fingerprint)"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_highlights_user_created "
                        "ON highlights (user_id, created_at)"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_highlights_user_source "
                        "ON highlights (user_id, source_id)"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_highlights_user_favorite "
                        "ON highlights (user_id, is_favorite)"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_highlights_user_device "
                        "ON highlights (user_id, device_id)"
                    )
                )
                conn.execute(text("PRAGMA foreign_keys=ON"))

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    if "collection_items" in tables or "collections" in tables:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS collection_items"))
            conn.execute(text("DROP TABLE IF EXISTS collections"))

    if "reminders" in tables:
        with engine.begin() as conn:
            conn.execute(text("DROP INDEX IF EXISTS ix_reminders_user_highlight_unique"))
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_reminders_user_highlight "
                    "ON reminders (user_id, highlight_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_reminders_user_remind_at "
                    "ON reminders (user_id, remind_at)"
                )
            )


def init_db_schema():
    Base.metadata.create_all(bind=engine)
    migrate_schema()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
