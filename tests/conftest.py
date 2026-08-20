"""Shared fixtures: in-memory DB session, and a hard sandbox around real data.

The sandbox matters as much as the DB override. Settings and the data-directory
constants are read live from the developer's own .env, so without redirecting
them a plain `pytest` run archives fake cards into the real collection folder
(often an iCloud directory), writes crops into data/crops, and runs schema
migrations against the real cards.db.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db import Base
from app import models  # noqa: F401 — registers ORM classes


@pytest.fixture(autouse=True)
def sandbox_data_dirs(tmp_path, monkeypatch):
    """Point every filesystem side effect at a temp directory for the test."""
    from app.routers import upload
    from app.services import cropping, photo_archive, ref_image

    # Namespaced so it never collides with a test's own use of tmp_path.
    root = tmp_path / "_sandbox"
    crops = root / "crops"
    inbox = root / "inbox"
    processed = inbox / "processed"
    refs = root / "ref_images"
    for path in (crops, processed, refs):
        path.mkdir(parents=True, exist_ok=True)

    # Each module imported its directory by value, so patch it where it is used.
    monkeypatch.setattr(cropping, "CROPS_DIR", crops)
    monkeypatch.setattr(ref_image, "REF_IMAGES_DIR", refs)
    monkeypatch.setattr(upload, "INBOX_DIR", inbox)
    monkeypatch.setattr(photo_archive, "INBOX_PROCESSED_DIR", processed)
    monkeypatch.setattr(photo_archive, "DATA_DIR", root)

    # Blank = archiving disabled, so nothing is ever copied to the real library.
    settings = get_settings()
    monkeypatch.setattr(settings, "collection_photos_dir", "")
    yield


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
