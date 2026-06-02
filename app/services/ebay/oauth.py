"""eBay OAuth token helpers (client-credentials + user refresh-token)."""
from __future__ import annotations

import base64

import httpx

from app.config import get_settings

SANDBOX_TOKEN_URL = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
LIVE_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"

SELL_SCOPE = "https://api.ebay.com/oauth/api_scope/sell.inventory"
# Base scope used for the Buy Browse API via the client-credentials grant.
BASE_SCOPE = "https://api.ebay.com/oauth/api_scope"
# Scope for the Buy Marketplace Insights API (real sold data; gated by approval).
INSIGHTS_SCOPE = "https://api.ebay.com/oauth/api_scope/buy.marketplace.insights"


def _basic_auth() -> str:
    s = get_settings()
    raw = f"{s.ebay_client_id}:{s.ebay_client_secret}".encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _token_url(live: bool) -> str:
    return LIVE_TOKEN_URL if live else SANDBOX_TOKEN_URL


def get_app_access_token(live: bool = True, scope: str = BASE_SCOPE) -> str:
    """Client-credentials (application) token for Buy APIs (Browse / Insights)."""
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
    return resp.json()["access_token"]


def get_user_access_token(live: bool = False) -> str:
    """Exchange the stored refresh token for a user access token (sell scope)."""
    s = get_settings()
    resp = httpx.post(
        _token_url(live),
        headers={
            "Authorization": f"Basic {_basic_auth()}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": s.ebay_user_refresh_token,
            "scope": SELL_SCOPE,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]
