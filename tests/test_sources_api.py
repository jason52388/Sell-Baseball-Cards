"""GET /api/sources/health: the UI's view of which price sources work."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.services import comp_sources


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    def override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    comp_sources.reset_health()
    # No lifespan: init_db would touch the real database.
    yield TestClient(app)
    app.dependency_overrides.clear()
    comp_sources.reset_health()


def test_health_is_empty_before_any_fetch(client):
    body = client.get("/api/sources/health").json()
    assert body == {"sources": [], "problems": [], "banner": None}


def test_health_reports_an_expired_token(client):
    comp_sources.record_status(comp_sources.SourceStatus(
        "sportscardspro", "auth_expired",
        "SportsCardsPro rejected the API token: Access token has expired",
    ))
    comp_sources.record_status(comp_sources.SourceStatus("130point", "ok", None, 4))
    body = client.get("/api/sources/health").json()
    by = {e["source"]: e for e in body["sources"]}
    assert by["sportscardspro"]["ok"] is False
    assert by["sportscardspro"]["state"] == "auth_expired"
    assert by["sportscardspro"]["last_error_at"]
    assert by["130point"]["ok"] is True and by["130point"]["last_success_at"]
    assert [p["source"] for p in body["problems"]] == ["sportscardspro"]
    assert "expired" in body["banner"]
