"""Get listing photos onto eBay without depending on the laptop tunnel.

Each local photo is uploaded to eBay Picture Services through the Commerce
Media API (createImageFromFile). eBay hosts the picture and returns its own
https URL, so a listing no longer breaks when the ngrok tunnel is down.

The returned URL is cached per local file (keyed by path, size and modified
time, and by sandbox vs live) in data/ebay_image_cache.json, so a retry or a
re-list does not upload the same photo again. An unused EPS image expires
(eBay returns `expirationDate`); an expired cache entry is re-uploaded.

Only when an upload fails does it fall back to the public tunnel URL
(PUBLIC_IMAGE_BASE_URL), and then each URL is checked first (https, HTTP 200,
an image content type) so a down tunnel fails the listing with a clear message
instead of publishing a listing with blank photos.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.config import DATA_DIR
from app.services.ebay.listing_common import public_crop_url

logger = logging.getLogger("ebay.media")

LIVE_MEDIA_API = "https://apim.ebay.com/commerce/media/v1_beta"
SANDBOX_MEDIA_API = "https://apim.sandbox.ebay.com/commerce/media/v1_beta"

CACHE_PATH = DATA_DIR / "ebay_image_cache.json"
_cache_lock = threading.Lock()


class ImageUnavailableError(RuntimeError):
    """A listing photo could not be put anywhere eBay can fetch it."""


def _media_base(live: bool) -> str:
    return LIVE_MEDIA_API if live else SANDBOX_MEDIA_API


def _cache_key(path: Path, live: bool) -> str:
    st = path.stat()
    return f"{'live' if live else 'sandbox'}|{path.resolve()}|{st.st_size}|{st.st_mtime_ns}"


def _load_cache() -> dict:
    try:
        return json.loads(Path(CACHE_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    path = Path(CACHE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _expired(entry: dict) -> bool:
    raw = entry.get("expires")
    if not raw:
        return False
    try:
        when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when <= datetime.now(timezone.utc)


def cached_url(path: str, live: bool) -> str | None:
    p = Path(path)
    if not p.exists():
        return None
    with _cache_lock:
        entry = _load_cache().get(_cache_key(p, live))
    if entry and entry.get("url") and not _expired(entry):
        return entry["url"]
    return None


def _remember(path: str, live: bool, url: str, expires: str | None) -> None:
    p = Path(path)
    with _cache_lock:
        cache = _load_cache()
        cache[_cache_key(p, live)] = {"url": url, "expires": expires}
        _save_cache(cache)


def _error_text(resp: httpx.Response) -> str:
    try:
        errs = resp.json().get("errors") or []
        msgs = [e.get("longMessage") or e.get("message") for e in errs if isinstance(e, dict)]
        if any(msgs):
            return "; ".join(m for m in msgs if m)
    except Exception:  # noqa: BLE001
        pass
    return f"HTTP {resp.status_code}"


def upload_image(path: str, token: str, live: bool) -> tuple[str, str | None]:
    """Upload one local photo to eBay Picture Services. Returns (url, expires)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"photo file missing: {p.name}")
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=60) as client:
        resp = client.post(
            f"{_media_base(live)}/image/create_image_from_file",
            headers=headers,
            files={"image": (p.name, p.read_bytes(), "image/jpeg")},
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"eBay photo upload failed: {_error_text(resp)}")
        body = {}
        try:
            body = resp.json() or {}
        except ValueError:
            pass
        if not body.get("imageUrl") and resp.headers.get("Location"):
            # The Location header is the getImage URI; it returns the imageUrl.
            got = client.get(resp.headers["Location"], headers=headers)
            if got.status_code >= 400:
                raise RuntimeError(f"eBay getImage failed: {_error_text(got)}")
            body = got.json() or {}
    url = body.get("imageUrl")
    if not url:
        raise RuntimeError("eBay photo upload returned no image URL")
    return url, body.get("expirationDate")


def check_public_url(url: str) -> str | None:
    """None when eBay could fetch this URL, else the reason it cannot."""
    if not url.startswith("https://"):
        return f"{url} is not https (eBay requires https photo URLs)"
    try:
        resp = httpx.head(url, follow_redirects=True, timeout=10)
    except httpx.HTTPError as exc:
        return f"{url} is unreachable ({exc.__class__.__name__})"
    if resp.status_code != 200:
        return f"{url} answered HTTP {resp.status_code}"
    ctype = resp.headers.get("content-type", "")
    if not ctype.startswith("image/"):
        return f"{url} is not an image (content type {ctype or 'missing'})"
    return None


def resolve_image_urls(paths: list[str], settings, live: bool, token_fn, *, required: bool) -> list[str]:
    """eBay-fetchable URLs for these local photos, in order.

    Per photo: cached eBay URL, else upload to eBay Picture Services, else the
    checked public tunnel URL. When `required` (live), any photo that cannot be
    placed fails the whole listing with ImageUnavailableError; otherwise (the
    sandbox) that photo is dropped with a warning.
    """
    urls: list[str] = []
    problems: list[str] = []
    token: str | None = None
    for path in paths:
        name = Path(path).name
        url = cached_url(path, live)
        reason = ""
        if not url and getattr(settings, "ebay_upload_images", True):
            try:
                token = token or token_fn()
                url, expires = upload_image(path, token, live)
                _remember(path, live, url, expires)
            except Exception as exc:  # noqa: BLE001
                reason = str(exc)
                logger.warning("eBay photo upload failed for %s: %s", name, exc)
        if not url:
            fallback = public_crop_url(path, settings.public_image_base_url)
            if not fallback:
                problems.append(
                    f"{name}: upload to eBay failed ({reason or 'uploads disabled'}) and "
                    "PUBLIC_IMAGE_BASE_URL is not set"
                )
            else:
                why = check_public_url(fallback)
                if why:
                    problems.append(f"{name}: upload to eBay failed ({reason or 'uploads disabled'}); {why}")
                else:
                    url = fallback
        if url:
            urls.append(url)
    if problems:
        msg = (
            "Could not get the photo(s) to eBay: " + " | ".join(problems)
            + ". If the tunnel is down, start it (tools/ebay_tunnel.sh) and try again."
        )
        if required:
            raise ImageUnavailableError(msg)
        logger.warning(msg)
    return urls
