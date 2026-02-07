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

    if "highlights" in tables:
        highlight_columns = {
            column["name"] for column in inspector.get_columns("highlights")
        }
        with engine.begin() as conn:
            if "original_text" not in highlight_columns:
                conn.execute(text("ALTER TABLE highlights ADD COLUMN original_text TEXT"))
            if "original_note" not in highlight_columns:
                conn.execute(text("ALTER TABLE highlights ADD COLUMN original_note TEXT"))
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
                    "SET original_note = note "
                    "WHERE original_note IS NULL"
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
