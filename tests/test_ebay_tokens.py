"""eBay user-token caching, scope handling, and the no-restart re-consent."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.routers import ebay_oauth
from app.services.ebay import oauth


class _Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body or {}
        self.text = text

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture(autouse=True)
def _fresh_cache():
    oauth.clear_token_cache()
    yield
    oauth.clear_token_cache()


def test_user_token_is_cached_until_expiry(monkeypatch):
    monkeypatch.setattr(get_settings(), "ebay_user_refresh_token", "rt-1")
    calls = []

    def fake_post(url, **kw):
        calls.append(kw["data"]["scope"])
        return _Resp(body={"access_token": f"tok{len(calls)}", "expires_in": 7200})

    monkeypatch.setattr(oauth.httpx, "post", fake_post)
    assert oauth.get_user_access_token(live=True) == "tok1"
    assert oauth.get_user_access_token(live=True) == "tok1"
    assert len(calls) == 1
    # A different scope (the sold-sync) is its own token.
    oauth.get_user_access_token(live=True, scope=oauth.FULFILLMENT_SCOPES)
    assert len(calls) == 2
    # A new refresh token (after re-consent) never reuses the old access token.
    monkeypatch.setattr(get_settings(), "ebay_user_refresh_token", "rt-2")
    assert oauth.get_user_access_token(live=True) == "tok3"


def test_missing_scope_explains_reconsent(monkeypatch):
    monkeypatch.setattr(
        oauth.httpx, "post", lambda url, **kw: _Resp(400, text='{"error":"invalid_scope"}'),
    )
    with pytest.raises(oauth.EbayScopeError, match="/ebay/oauth/start"):
        oauth.get_user_access_token(live=True, scope=oauth.FULFILLMENT_SCOPES)


def test_consent_asks_for_fulfillment_but_listing_tokens_do_not():
    assert oauth.SELL_FULFILLMENT_SCOPE in oauth.CONSENT_SCOPES
    # Listing keeps working with a refresh token minted before fulfillment.
    assert oauth.SELL_FULFILLMENT_SCOPE not in oauth.USER_SCOPES


def test_refresh_expiry_warning(monkeypatch, caplog):
    soon = (datetime.now(timezone.utc) + timedelta(days=10, hours=1)).isoformat()
    monkeypatch.setattr(get_settings(), "ebay_user_refresh_token_expires_at", soon)
    assert oauth.refresh_token_days_left() == 10
    oauth._warn_if_refresh_expiring(get_settings())
    assert "expires in" in caplog.text


def test_callback_writes_token_and_reloads_settings(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("EBAY_MODE=live\n")
    monkeypatch.setattr(ebay_oauth, "_ENV_PATH", env)
    reloaded = []
    monkeypatch.setattr(ebay_oauth, "_reload_settings", lambda: reloaded.append(True))
    monkeypatch.setattr(
        ebay_oauth.oauth, "exchange_code_for_refresh_token",
        lambda code, live: {"refresh_token": "new-rt", "refresh_token_expires_in": 47304000},
    )
    ebay_oauth.reset_pending_states()
    state = ebay_oauth._issue_state()
    resp = TestClient(app).get("/ebay/oauth/callback", params={"code": "c", "state": state})
    assert resp.status_code == 200
    text = env.read_text()
    assert "EBAY_USER_REFRESH_TOKEN=new-rt" in text
    assert "EBAY_USER_REFRESH_TOKEN_EXPIRES_AT=" in text
    assert reloaded == [True]
    assert "no restart" in resp.text
