"""Download a marketplace reference photo locally so the UI doesn't hot-link
eBay's CDN (whose URLs rotate/expire). Best-effort: on any failure we keep the
original remote URL rather than break pricing.
"""
from __future__ import annotations

import logging

import httpx

from app.config import REF_IMAGES_DIR, get_settings

logger = logging.getLogger("ref_image")

# Path the app serves these from (mounted in app.main).
_SERVE_PREFIX = "/refimg"


def localize(card_id: int, url: str | None) -> str | None:
    """Download `url` into data/ref_images/<card_id>.jpg and return the local
    serve path (e.g. "/refimg/12.jpg"). Returns the original url on failure, or
    the url unchanged if localization is disabled / already local."""
    if not url or not get_settings().localize_reference_images:
        return url
    if url.startswith(_SERVE_PREFIX):
        return url  # already localized
    try:
        resp = httpx.get(url, timeout=20, follow_redirects=True)
        resp.raise_for_status()
        dest = REF_IMAGES_DIR / f"{card_id}.jpg"
        dest.write_bytes(resp.content)
        return f"{_SERVE_PREFIX}/{card_id}.jpg"
    except Exception:  # noqa: BLE001
        logger.warning("reference image localize failed for card %s (%s)", card_id, url)
        return url
