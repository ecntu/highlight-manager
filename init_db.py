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
    Base.metadata.create_all(bind=engine)


if __name__ == "__main__":
    init_db()
    print("Database initialized successfully")
