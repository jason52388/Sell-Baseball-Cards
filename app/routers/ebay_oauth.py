"""One-time eBay user-consent flow to mint EBAY_USER_REFRESH_TOKEN.

Visit /ebay/oauth/start in a browser → you're sent to eBay to log in and grant
the app permission → eBay redirects back to /ebay/oauth/callback with a code →
we exchange it for a refresh token and write it into .env.

Requires EBAY_RU_NAME to be set, and the RuName's "auth accepted URL" in the
eBay portal must point at <public URL>/ebay/oauth/callback.
"""
from __future__ import annotations

import html
import logging
import re
import secrets

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import ROOT_DIR, get_settings
from app.services.ebay import oauth

logger = logging.getLogger("ebay.oauth")

router = APIRouter(prefix="/ebay/oauth", tags=["ebay"])

_ENV_PATH = ROOT_DIR / ".env"

# States handed out by /start and not yet redeemed. The callback writes a
# refresh token into .env, and the app is unauthenticated and internet-reachable
# while the listing tunnel is up, so a callback must only be honoured when it
# answers a consent flow this app actually began.
_PENDING_STATES: set[str] = set()
_MAX_PENDING_STATES = 16


def reset_pending_states() -> None:
    """Drop any outstanding consent states (used by tests)."""
    _PENDING_STATES.clear()


def _issue_state() -> str:
    if len(_PENDING_STATES) >= _MAX_PENDING_STATES:
        _PENDING_STATES.clear()  # abandoned flows, not real pending consents
    state = secrets.token_urlsafe(24)
    _PENDING_STATES.add(state)
    return state


def _error_page(title: str, detail: str, status: int) -> HTMLResponse:
    """Render an error page. `detail` can come straight from the query string,
    so it is escaped rather than interpolated as markup."""
    return HTMLResponse(
        f"<h3>{html.escape(title)}</h3><pre>{html.escape(detail)}</pre>",
        status_code=status,
    )


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
    # Production keyset is used for both "preview" and "live"; only the explicit
    # "sandbox" mode talks to eBay's sandbox auth servers.
    live = s.ebay_mode.lower() != "sandbox"
    return RedirectResponse(oauth.build_consent_url(live=live, state=_issue_state()))


@router.get("/callback")
def callback(
    code: str | None = None, error: str | None = None, state: str | None = None
) -> HTMLResponse:
    if error or not code:
        return _error_page("eBay consent failed", error or "no code returned", 400)
    # Only redeem a state this app handed out, and only once.
    if not state or state not in _PENDING_STATES:
        logger.warning("rejected eBay OAuth callback with an unrecognised state")
        return _error_page(
            "eBay consent rejected",
            "This callback did not come from a consent flow started here "
            "(missing or unrecognised state). Start again at /ebay/oauth/start.",
            400,
        )
    _PENDING_STATES.discard(state)
    s = get_settings()
    # Production keyset is used for both "preview" and "live"; only the explicit
    # "sandbox" mode talks to eBay's sandbox auth servers.
    live = s.ebay_mode.lower() != "sandbox"
    try:
        body = oauth.exchange_code_for_refresh_token(code, live=live)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Token exchange failed")
        return _error_page("Token exchange failed", str(exc), 500)

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
