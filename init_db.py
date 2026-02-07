from app.database import init_db_schema
from app.models import (
    User,
    Device,
    Source,
    Highlight,
    Reminder,
    Tag,
    HighlightTag,
    Collection,
    CollectionItem,
    HighlightLink,
    DigestConfig,
)
def init_db():
    init_db_schema()


if __name__ == "__main__":
    init_db()
    print("Database initialized successfully")
