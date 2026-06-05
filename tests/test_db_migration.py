"""Additive SQLite column migration for existing DBs (create_all never ALTERs)."""
from sqlalchemy import create_engine, text


def test_ensure_sqlite_columns_adds_missing(monkeypatch, tmp_path):
    import app.db as db

    url = f"sqlite:///{tmp_path / 'old.db'}"
    eng = create_engine(url, connect_args={"check_same_thread": False})
    # Simulate a pre-existing DB whose comps table lacks the new column.
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE comps (id INTEGER PRIMARY KEY, source VARCHAR(48))"
        )

    monkeypatch.setattr(db, "engine", eng)
    monkeypatch.setattr(db._settings, "database_url", url)
    db._ensure_columns()

    with eng.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(comps)")}
    assert "marketplace" in cols

    # Idempotent: a second run must not error or duplicate.
    db._ensure_columns()
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO comps (source, marketplace) VALUES ('x','eBay')"))
        row = conn.execute(text("SELECT marketplace FROM comps")).one()
    assert row[0] == "eBay"
