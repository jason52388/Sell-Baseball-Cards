"""OAuth consent flow: CSRF state and error-page escaping.

These matter because the app is deliberately exposed to the internet through the
ngrok tunnel while listing (eBay must fetch crop images), and it has no auth of
its own. A callback that accepts any `code` lets a stranger's refresh token be
written into .env, so listings would publish under an account you don't control.
"""
from fastapi.testclient import TestClient

from app.main import app
from app.routers import ebay_oauth


def _client():
    return TestClient(app)


def test_callback_rejects_a_code_that_was_not_issued_by_our_own_start():
    resp = _client().get(
        "/ebay/oauth/callback", params={"code": "attacker-code", "state": "forged"}
    )
    assert resp.status_code == 400
    assert "state" in resp.text.lower()


def test_callback_rejects_a_missing_state():
    resp = _client().get("/ebay/oauth/callback", params={"code": "attacker-code"})
    assert resp.status_code == 400


def test_each_consent_url_carries_a_fresh_unguessable_state(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ebay_ru_name", "Test-RuName")
    monkeypatch.setattr(get_settings(), "ebay_client_id", "cid")
    ebay_oauth.reset_pending_states()

    first = _client().get("/ebay/oauth/start", follow_redirects=False)
    second = _client().get("/ebay/oauth/start", follow_redirects=False)
    assert first.status_code == 307 and second.status_code == 307

    def state_of(resp):
        from urllib.parse import parse_qs, urlparse

        return parse_qs(urlparse(resp.headers["location"]).query)["state"][0]

    a, b = state_of(first), state_of(second)
    assert a != b, "a constant state gives no CSRF protection"
    assert len(a) >= 16


def test_error_text_from_the_url_is_escaped_not_rendered():
    resp = _client().get(
        "/ebay/oauth/callback", params={"error": "<script>alert(1)</script>"}
    )
    assert resp.status_code == 400
    assert "<script>" not in resp.text
    assert "&lt;script&gt;" in resp.text
