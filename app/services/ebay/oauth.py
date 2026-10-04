"""eBay OAuth token helpers (client-credentials + user refresh-token)."""
from __future__ import annotations

import base64
import logging
import threading
import time
from datetime import datetime, timezone

import httpx

from app.config import get_settings

logger = logging.getLogger("ebay.oauth")

SANDBOX_TOKEN_URL = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
LIVE_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"

SELL_SCOPE = "https://api.ebay.com/oauth/api_scope/sell.inventory"
SELL_ACCOUNT_SCOPE = "https://api.ebay.com/oauth/api_scope/sell.account"
# Fulfillment lets the sold-sync read orders (getOrders) to learn what sold.
SELL_FULFILLMENT_SCOPE = "https://api.ebay.com/oauth/api_scope/sell.fulfillment"
# Scope set for listing calls: inventory lets us create/publish offers (and
# upload photos through the Media API); account lets us create/read business
# policies + inventory locations. Space-separated per the OAuth spec.
USER_SCOPES = f"{SELL_SCOPE} {SELL_ACCOUNT_SCOPE}"
# Scope set requested during user CONSENT: everything above plus fulfillment.
# A refresh-token exchange may only ask for scopes the user consented to, so the
# listing calls keep asking for USER_SCOPES (which works with a refresh token
# minted before fulfillment was added) and only the sold-sync asks for
# fulfillment. A token minted before this change needs one re-consent at
# /ebay/oauth/start before the sold-sync works.
CONSENT_SCOPES = f"{USER_SCOPES} {SELL_FULFILLMENT_SCOPE}"
FULFILLMENT_SCOPES = SELL_FULFILLMENT_SCOPE
# Base scope used for the Buy Browse API via the client-credentials grant.
BASE_SCOPE = "https://api.ebay.com/oauth/api_scope"
# Scope for the Buy Marketplace Insights API (real sold data; gated by approval).
INSIGHTS_SCOPE = "https://api.ebay.com/oauth/api_scope/buy.marketplace.insights"

# Where a human is sent to grant consent (authorization-code grant).
SANDBOX_AUTHORIZE_URL = "https://auth.sandbox.ebay.com/oauth2/authorize"
LIVE_AUTHORIZE_URL = "https://auth.ebay.com/oauth2/authorize"


def _basic_auth() -> str:
    s = get_settings()
    raw = f"{s.ebay_client_id}:{s.ebay_client_secret}".encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _token_url(live: bool) -> str:
    return LIVE_TOKEN_URL if live else SANDBOX_TOKEN_URL


# In-memory cache for client-credentials tokens. eBay app tokens last ~2h; we
# reuse them until shortly before expiry instead of fetching one per API call,
# which roughly halves our HTTP traffic to eBay and avoids needlessly hammering
# the OAuth token endpoint (which has its own throttle). Keyed by (live, scope).
_TOKEN_EXPIRY_MARGIN = 60  # refresh this many seconds before the token expires
_token_cache: dict[tuple[bool, str], tuple[str, float]] = {}
_token_lock = threading.Lock()


def get_app_access_token(live: bool = True, scope: str = BASE_SCOPE) -> str:
    """Client-credentials (application) token for Buy APIs (Browse / Insights).

    Cached in-memory until ~1 minute before expiry; callers can invoke this on
    every request without incurring a token fetch each time.
    """
    key = (live, scope)
    now = time.monotonic()
    cached = _token_cache.get(key)
    if cached is not None and cached[1] > now:
        return cached[0]

    with _token_lock:
        # Re-check inside the lock in case another thread just refreshed it.
        cached = _token_cache.get(key)
        if cached is not None and cached[1] > time.monotonic():
            return cached[0]

        resp = httpx.post(
            _token_url(live),
            headers={
                "Authorization": f"Basic {_basic_auth()}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": scope},
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        token = body["access_token"]
        # expires_in is seconds from now; default to 2h if absent.
        ttl = int(body.get("expires_in", 7200)) - _TOKEN_EXPIRY_MARGIN
        _token_cache[key] = (token, time.monotonic() + max(ttl, 0))
        return token


class EbayScopeError(RuntimeError):
    """The stored refresh token was not granted the scope a call needs."""


# User access tokens last ~2h. Cached per (live, scope, refresh token) so a
# batch of 50 listings makes one token call instead of 50; a new refresh token
# (after re-consent) is a new key, so it never reuses a stale token.
_user_token_cache: dict[tuple[bool, str, str], tuple[str, float]] = {}
_REFRESH_WARN_DAYS = 30


def clear_token_cache() -> None:
    """Forget cached app and user tokens (after re-consent, and in tests)."""
    _token_cache.clear()
    _user_token_cache.clear()


def refresh_token_days_left(s=None) -> int | None:
    """Days until the stored refresh token expires, when the consent callback
    recorded it (EBAY_USER_REFRESH_TOKEN_EXPIRES_AT), else None."""
    s = s or get_settings()
    raw = getattr(s, "ebay_user_refresh_token_expires_at", "") or ""
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (when - datetime.now(timezone.utc)).days


def _warn_if_refresh_expiring(s) -> None:
    days = refresh_token_days_left(s)
    if days is not None and days <= _REFRESH_WARN_DAYS:
        logger.warning(
            "eBay refresh token expires in %d day(s). Re-consent at "
            "/ebay/oauth/start before then or listing will stop working.", days,
        )


def get_user_access_token(live: bool = False, scope: str = USER_SCOPES) -> str:
    """Exchange the stored refresh token for a user access token, cached until
    shortly before it expires.

    Defaults to the inventory+account scope set so the same token can create
    listings AND manage business policies / locations.
    """
    s = get_settings()
    key = (live, scope, s.ebay_user_refresh_token)
    cached = _user_token_cache.get(key)
    if cached is not None and cached[1] > time.monotonic():
        return cached[0]

    with _token_lock:
        cached = _user_token_cache.get(key)
        if cached is not None and cached[1] > time.monotonic():
            return cached[0]
        _warn_if_refresh_expiring(s)
        resp = httpx.post(
            _token_url(live),
            headers={
                "Authorization": f"Basic {_basic_auth()}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": s.ebay_user_refresh_token,
                "scope": scope,
            },
            timeout=30,
        )
        if resp.status_code == 400 and "invalid_scope" in resp.text:
            raise EbayScopeError(
                "Your eBay authorization does not include the permission this "
                "needs. Visit /ebay/oauth/start once to re-authorize the app."
            )
        resp.raise_for_status()
        body = resp.json()
        token = body["access_token"]
        ttl = int(body.get("expires_in", 7200)) - _TOKEN_EXPIRY_MARGIN
        _user_token_cache[key] = (token, time.monotonic() + max(ttl, 0))
        return token


def _authorize_url(live: bool) -> str:
    return LIVE_AUTHORIZE_URL if live else SANDBOX_AUTHORIZE_URL


def build_consent_url(live: bool = True, state: str = "setup") -> str:
    """URL a human visits to grant this app permission to act on their account.

    redirect_uri is the eBay RuName (not the raw https URL); eBay redirects the
    browser to the RuName's configured "auth accepted URL" with a `code`.
    """
    s = get_settings()
    from urllib.parse import urlencode

    params = {
        "client_id": s.ebay_client_id,
        "redirect_uri": s.ebay_ru_name,
        "response_type": "code",
        "scope": CONSENT_SCOPES,
        "state": state,
    }
    return f"{_authorize_url(live)}?{urlencode(params)}"


def exchange_code_for_refresh_token(code: str, live: bool = True) -> dict:
    """Exchange an authorization code for tokens. Returns the full token body
    (includes `refresh_token`, `access_token`, `refresh_token_expires_in`)."""
    s = get_settings()
    resp = httpx.post(
        _token_url(live),
        headers={
            "Authorization": f"Basic {_basic_auth()}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": s.ebay_ru_name,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()
