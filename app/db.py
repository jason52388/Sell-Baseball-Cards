"""SQLAlchemy engine, session factory, and Base."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()

# check_same_thread=False is required for SQLite under FastAPI's threadpool.
engine = create_engine(
    _settings.database_url,
    connect_args={"check_same_thread": False}
    if _settings.database_url.startswith("sqlite")
    else {},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """Create tables. Importing models registers them on Base.metadata."""
    from app import models  # noqa: F401  (registers ORM classes)

    Base.metadata.create_all(bind=engine)
    _ensure_columns()


# Columns added after a table's first release. create_all() never alters an
# existing table, so this additively backfills them on SQLite (an ALTER ADD
# COLUMN is cheap and idempotent here because we check before adding).
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "cards": [("batch_tag", "VARCHAR(128)"), ("sport", "VARCHAR(32)")],
    "image_uploads": [("batch_tag", "VARCHAR(128)")],
}


def _ensure_columns() -> None:
    if not _settings.database_url.startswith("sqlite"):
        return
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, cols in _ADDED_COLUMNS.items():
            if table not in tables:
                continue
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols:
                if name not in have:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def get_db() -> Iterator[Session]:
    """FastAPI dependency that yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
