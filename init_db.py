from app.database import init_db_schema
def init_db():
    init_db_schema()


if __name__ == "__main__":
    init_db()
    print("Database initialized successfully")
