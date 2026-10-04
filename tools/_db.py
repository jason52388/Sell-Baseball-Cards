"""Open a session on a cards.db for the one-off tools.

The tools may run before the app has been restarted on new code, so the
database is first brought up to the current schema the same way the app does
at startup: new tables are created and new columns added. Both steps are
additive; nothing existing is changed or removed.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app import models  # noqa: F401  (registers ORM classes)
from app.db import _ADDED_COLUMNS, Base


def open_session(data_dir: Path) -> Session:
    db_path = Path(data_dir).resolve() / "cards.db"
    if not db_path.exists():
        raise SystemExit(f"No database at {db_path}")
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
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
    return sessionmaker(bind=engine, expire_on_commit=False)()
