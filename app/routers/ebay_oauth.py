"""One-time eBay user-consent flow to mint EBAY_USER_REFRESH_TOKEN.

Visit /ebay/oauth/start in a browser → you're sent to eBay to log in and grant
the app permission → eBay redirects back to /ebay/oauth/callback with a code →
we exchange it for a refresh token and write it into .env.

Requires EBAY_RU_NAME to be set, and the RuName's "auth accepted URL" in the
eBay portal must point at <public URL>/ebay/oauth/callback.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import ROOT_DIR, get_settings
from app.services.ebay import oauth

logger = logging.getLogger("ebay.oauth")

router = APIRouter(prefix="/ebay/oauth", tags=["ebay"])

_ENV_PATH = ROOT_DIR / ".env"


def _write_env_value(key: str, value: str) -> None:
    """Set KEY=value in .env (replace existing line or append)."""
    text = _ENV_PATH.read_text(encoding="utf-8") if _ENV_PATH.exists() else ""
    line = f"{key}={value}"
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(line, text)
    else:
        text = text.rstrip("\n") + f"\n{line}\n"
    _ENV_PATH.write_text(text, encoding="utf-8")


@router.get("/start")
def start() -> RedirectResponse:
    s = get_settings()
    if not s.ebay_ru_name:
        return HTMLResponse(
            "<h3>EBAY_RU_NAME is not set.</h3><p>Create a redirect URL (RuName) "
            "in the eBay developer portal, set it in .env, and restart.</p>",
            status_code=400,
        )
    live = s.ebay_mode.lower() == "live"
    return RedirectResponse(oauth.build_consent_url(live=live))


@router.get("/callback")
def callback(code: str | None = None, error: str | None = None) -> HTMLResponse:
    if error or not code:
        return HTMLResponse(
            f"<h3>eBay consent failed</h3><pre>{error or 'no code returned'}</pre>",
            status_code=400,
        )
    s = get_settings()
    live = s.ebay_mode.lower() == "live"
    try:
        body = oauth.exchange_code_for_refresh_token(code, live=live)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Token exchange failed")
        return HTMLResponse(
            f"<h3>Token exchange failed</h3><pre>{exc}</pre>", status_code=500
        )

    refresh = body.get("refresh_token", "")
    if refresh:
        _write_env_value("EBAY_USER_REFRESH_TOKEN", refresh)
    days = round(int(body.get("refresh_token_expires_in", 0)) / 86400)
    logger.info("eBay refresh token obtained and written to .env (valid ~%sd)", days)
    return HTMLResponse(
        "<h2>✅ eBay authorization complete</h2>"
        f"<p>Refresh token saved to <code>.env</code> (valid ~{days} days). "
        "Restart the app to pick it up. You can close this tab.</p>"
    )
