"""SQLAlchemy engine, session factory, and Base."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
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

if _settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver hook
        """WAL lets reads continue during a write, and the busy timeout waits for
        a held lock instead of failing instantly. An upload holds its write
        transaction across minutes of vision and pricing calls, so without these
        a concurrent request dies on "database is locked"."""
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.close()


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
    "cards": [
        ("batch_tag", "VARCHAR(128)"),
        ("sport", "VARCHAR(32)"),
        ("side", "VARCHAR(8) DEFAULT 'front'"),
        ("back_crop_path", "VARCHAR(512)"),
        ("back_identification_json", "TEXT"),
        ("sold_max_estimate", "FLOAT"),
        ("photo_taken_at", "DATETIME"),
        ("photo_quality", "VARCHAR(32)"),
        ("pre_pair_identity_json", "TEXT"),
    ],
    "image_uploads": [("batch_tag", "VARCHAR(128)")],
    "comps": [("marketplace", "VARCHAR(32)")],
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
