"""Download a marketplace reference photo locally so the UI doesn't hot-link
eBay's CDN (whose URLs rotate/expire). Best-effort: on any failure we keep the
original remote URL rather than break pricing.
"""
from __future__ import annotations

import logging
import uuid

import httpx

from app.config import REF_IMAGES_DIR, get_settings

logger = logging.getLogger("ref_image")

# Path the app serves these from (mounted in app.main).
_SERVE_PREFIX = "/refimg"


def localize(card_id: int, url: str | None) -> str | None:
    """Download `url` into data/ref_images/<card_id>-<token>.jpg and return the
    local serve path (e.g. "/refimg/12-ab12cd34.jpg"). Returns the original url
    on failure, or unchanged if disabled / already local.

    The unique token means a re-priced card's NEW reference image gets a NEW
    filename, so a browser never shows a stale cached copy of the old one."""
    if not url or not get_settings().localize_reference_images:
        return url
    if url.startswith(_SERVE_PREFIX):
        return url  # already localized
    try:
        resp = httpx.get(url, timeout=20, follow_redirects=True)
        resp.raise_for_status()
        name = f"{card_id}-{uuid.uuid4().hex[:8]}.jpg"
        (REF_IMAGES_DIR / name).write_bytes(resp.content)
        # Best-effort: clear out any older ref images for this card so they don't
        # pile up (the card only ever shows the latest).
        for old in REF_IMAGES_DIR.glob(f"{card_id}-*.jpg"):
            if old.name != name:
                try:
                    old.unlink()
                except OSError:
                    pass
        return f"{_SERVE_PREFIX}/{name}"
    except Exception:  # noqa: BLE001
        logger.warning("reference image localize failed for card %s (%s)", card_id, url)
        return url
