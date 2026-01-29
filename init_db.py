from app.database import engine, Base
from app.models import (
    User,
    Device,
    Source,
    Highlight,
    Tag,
    HighlightTag,
    Collection,
    CollectionItem,
    HighlightLink,
    DigestConfig,
)


def init_db():
    # Drop and recreate sources table to match new schema
    from sqlalchemy import text

    with engine.connect() as conn:
        conn.execute(text("DROP TABLE IF EXISTS sources CASCADE"))
        conn.commit()

    Base.metadata.create_all(bind=engine)


if __name__ == "__main__":
    init_db()
    print("Database initialized successfully")
