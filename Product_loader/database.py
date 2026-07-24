from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = "sqlite:///database.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def run_migrations():
    """create_all() only creates tables that don't exist yet — it never
    alters an existing table. This adds any model columns that are
    missing from the actual SQLite file, so pulling in schema changes
    (like deals_queue.old_price) doesn't require deleting database.db.

    SQLite only supports simple `ADD COLUMN` (no type change, no drop),
    which is all we need here since every added column so far is
    nullable.
    """
    inspector = inspect(engine)

    if not inspector.get_table_names():
        # Nothing exists yet — create_all() will build everything fresh.
        return

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in inspector.get_table_names():
                continue  # brand-new table — create_all() handles it

            existing_cols = {
                col["name"] for col in inspector.get_columns(table.name)
            }

            for column in table.columns:
                if column.name in existing_cols:
                    continue

                col_type = column.type.compile(dialect=engine.dialect)
                conn.execute(
                    text(
                        f'ALTER TABLE "{table.name}" '
                        f'ADD COLUMN "{column.name}" {col_type}'
                    )
                )
                print(f"Migrated: added {table.name}.{column.name}")