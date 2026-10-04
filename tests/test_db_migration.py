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


def test_listings_gain_end_and_sold_columns(monkeypatch, tmp_path):
    import app.db as db

    url = f"sqlite:///{tmp_path / 'old.db'}"
    eng = create_engine(url, connect_args={"check_same_thread": False})
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE listings (id INTEGER PRIMARY KEY, card_id INTEGER, "
            "ebay_mode VARCHAR(16), status VARCHAR(16))"
        )
    monkeypatch.setattr(db, "engine", eng)
    monkeypatch.setattr(db._settings, "database_url", url)
    db._ensure_columns()
    with eng.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(listings)")}
    assert {"ended_at", "sold_at", "sold_price", "order_id"} <= cols


def test_cards_and_uploads_gain_soft_delete_and_hash_columns(monkeypatch, tmp_path):
    import app.db as db

    url = f"sqlite:///{tmp_path / 'old.db'}"
    eng = create_engine(url, connect_args={"check_same_thread": False})
    with eng.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE cards (id INTEGER PRIMARY KEY, status VARCHAR(32))")
        conn.exec_driver_sql("CREATE TABLE image_uploads (id INTEGER PRIMARY KEY, filename VARCHAR(512))")
    monkeypatch.setattr(db, "engine", eng)
    monkeypatch.setattr(db._settings, "database_url", url)
    db._ensure_columns()
    with eng.begin() as conn:
        cards = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(cards)")}
        ups = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(image_uploads)")}
    assert {"deleted_at", "status_before_delete"} <= cards
    assert {"stored_name", "sha256"} <= ups
