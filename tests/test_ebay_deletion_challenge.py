"""eBay account-deletion challenge: the hash, and parity between the two copies.

If this response is ever wrong, eBay marks the endpoint down and deactivates the
production keyset. The logic exists twice — in the app router and in the
standalone VPS responder — with nothing but a comment keeping them in step, so
both are pinned here against an independently computed digest.
"""
import hashlib
import importlib.util

from fastapi.testclient import TestClient

from app.config import ROOT_DIR, get_settings
from app.main import app

CODE = "abc123challenge"
TOKEN = "a-verification-token-at-least-32-chars-long"
ENDPOINT = "https://ebay.example.com/ebay/account-deletion"

# eBay's documented order: challengeCode + verificationToken + endpoint.
EXPECTED = hashlib.sha256(
    CODE.encode() + TOKEN.encode() + ENDPOINT.encode()
).hexdigest()


def _load_responder():
    """Import the VPS responder module straight from the deploy directory."""
    path = ROOT_DIR / "deploy" / "ebay-deletion" / "responder.py"
    spec = importlib.util.spec_from_file_location("vps_responder", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_app_answers_the_challenge_with_the_documented_hash(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ebay_verification_token", TOKEN)
    monkeypatch.setattr(settings, "ebay_deletion_endpoint_url", ENDPOINT)

    resp = TestClient(app).get(
        "/ebay/account-deletion", params={"challenge_code": CODE}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == {"challengeResponse": EXPECTED}


def test_vps_responder_computes_the_same_digest(monkeypatch):
    """The deploy copy must not drift from the app copy."""
    responder = _load_responder()
    monkeypatch.setattr(responder, "TOKEN", TOKEN)
    monkeypatch.setattr(responder, "ENDPOINT", ENDPOINT)

    h = hashlib.sha256()
    h.update(CODE.encode("utf-8"))
    h.update(responder.TOKEN.encode("utf-8"))
    h.update(responder.ENDPOINT.encode("utf-8"))
    assert h.hexdigest() == EXPECTED


def test_challenge_is_refused_when_the_token_is_not_configured(monkeypatch):
    """Better a loud 500 than a confidently wrong digest."""
    settings = get_settings()
    monkeypatch.setattr(settings, "ebay_verification_token", "")
    monkeypatch.setattr(settings, "ebay_deletion_endpoint_url", ENDPOINT)

    resp = TestClient(app).get(
        "/ebay/account-deletion", params={"challenge_code": CODE}
    )
    assert resp.status_code == 500


def test_deploy_responder_file_stays_dependency_free():
    """It ships in a stdlib-only alpine image; an import would break the deploy."""
    source = (ROOT_DIR / "deploy" / "ebay-deletion" / "responder.py").read_text()
    for forbidden in ("import httpx", "import fastapi", "from app."):
        assert forbidden not in source
