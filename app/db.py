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
    _ensure_sqlite_columns()


# Lightweight, additive migrations for SQLite (create_all never ALTERs existing
# tables). Each entry: table -> {column: SQL type}. Adding a column is safe and
# idempotent; we only ADD what's missing. Keep in sync with the models above.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "comps": {"marketplace": "VARCHAR(32)"},
}


def _ensure_sqlite_columns() -> None:
    if engine.url.get_backend_name() != "sqlite":
        return
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if not existing:
                continue  # table absent (create_all just made it with all columns)
            for name, sqltype in columns.items():
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {sqltype}")


def get_db() -> Iterator[Session]:
    """FastAPI dependency that yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
