"""A rejected SportsCardsPro token must be visible, and never logged in full.

Both matter for the same reason: when the token silently expired, every card
looked like "no marketplace match" and the real cause (an expired subscription)
was invisible, while the one place it did surface printed the whole token into a
traceback.
"""
import httpx
import pytest

from app.config import get_settings
from app.services import comp_sources, pricecharting

TOKEN = "sekrit-token-value-abc123"

EXPIRED_BODY = {
    "error": "Access token has expired",
    "error-message": "Access token has expired",
    "status": "error",
}
UNKNOWN_BODY = {
    "error": "Unknown access token",
    "error-message": "Unknown access token",
    "status": "error",
}


@pytest.fixture
def token_set(monkeypatch):
    monkeypatch.setattr(get_settings(), "pricecharting_token", TOKEN)
    return TOKEN


def _respond(monkeypatch, status, body):
    """Answer the SportsCardsPro API with this status/body.

    httpx is a shared module, so the fake stays URL-aware: only the pricing
    catalogue gets the error, everything else returns an empty 200.
    """
    def fake_get(url, **kw):
        request = httpx.Request("GET", f"{url}?t={TOKEN}&q=x")
        if "sportscardspro.com/api" in url or "pricecharting.com/api" in url:
            return httpx.Response(status, json=body, request=request)
        return httpx.Response(200, text="", request=request)
    monkeypatch.setattr(pricecharting.httpx, "get", fake_get)


def test_expired_token_raises_a_named_error(token_set, monkeypatch):
    _respond(monkeypatch, 410, EXPIRED_BODY)
    with pytest.raises(pricecharting.PriceChartingAuthError):
        pricecharting.fetch_comps("1989 Upper Deck Ken Griffey Jr #1")


def test_unknown_token_raises_a_named_error(token_set, monkeypatch):
    _respond(monkeypatch, 403, UNKNOWN_BODY)
    with pytest.raises(pricecharting.PriceChartingAuthError):
        pricecharting.fetch_comps("1989 Upper Deck Ken Griffey Jr #1")


def test_the_error_says_what_is_wrong_without_leaking_the_token(token_set, monkeypatch):
    _respond(monkeypatch, 410, EXPIRED_BODY)
    with pytest.raises(pricecharting.PriceChartingAuthError) as exc:
        pricecharting.fetch_comps("x")
    message = str(exc.value)
    assert TOKEN not in message, "the token must never appear in an error"
    assert "expired" in message.lower()


def test_redaction_strips_the_token_from_any_text(token_set):
    leaky = f"https://www.sportscardspro.com/api/products?t={TOKEN}&q=griffey"
    cleaned = pricecharting.redact_token(leaky)
    assert TOKEN not in cleaned
    assert "sportscardspro.com" in cleaned  # still useful for debugging


def test_redaction_works_even_for_a_different_token_in_the_url(token_set):
    cleaned = pricecharting.redact_token("GET /api/products?t=some-other-token&q=x failed")
    assert "some-other-token" not in cleaned


def test_a_rejected_token_surfaces_as_a_note_on_the_card(token_set, monkeypatch):
    """gather_comps must report WHY prices are missing, not swallow it."""
    _respond(monkeypatch, 410, EXPIRED_BODY)
    monkeypatch.setattr(comp_sources.browse, "has_credentials", lambda: False)
    monkeypatch.setattr(comp_sources.point130, "is_enabled", lambda: False)
    monkeypatch.setattr(comp_sources.browser_scrape, "is_enabled", lambda: False)

    comps, notes = comp_sources.gather_comps("1989 Upper Deck Ken Griffey Jr #1", refresh=True)
    joined = " ".join(notes).lower()
    assert "sportscardspro" in joined or "pricecharting" in joined
    assert "expired" in joined
    assert TOKEN not in " ".join(notes)
