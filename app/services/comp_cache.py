"""Persistent cache of pooled comps, keyed by card identity.

Pricing the same card identity (across uploads and app restarts) reuses a stored
result within settings.price_cache_ttl_days instead of re-querying eBay Browse /
PriceCharting / etc. Stores the pooled SoldComp list as JSON in the price_cache
table. Best-effort: any DB hiccup degrades to "no cache" rather than failing a
price.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, fields
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.db import SessionLocal
from app.services.ebay.base import SoldComp

logger = logging.getLogger("comp_cache")

_COMP_FIELDS = {f.name for f in fields(SoldComp)}


def _key(query: str, graded: bool, marketplace: str) -> str:
    norm = re.sub(r"\s+", " ", (query or "").strip().lower())
    return f"{marketplace}|{'graded' if graded else 'raw'}|{norm}"


def _to_comp(d: dict) -> SoldComp:
    # Tolerate schema drift: drop unknown keys.
    return SoldComp(**{k: v for k, v in d.items() if k in _COMP_FIELDS})


def get(query: str, *, graded: bool, marketplace: str) -> list[SoldComp] | None:
    """Return cached comps if a fresh entry exists, else None."""
    ttl_days = get_settings().price_cache_ttl_days
    if ttl_days <= 0:
        return None
    from app.models import PriceCache

    key = _key(query, graded, marketplace)
    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
    try:
        with SessionLocal() as db:
            row = db.query(PriceCache).filter(PriceCache.query_key == key).one_or_none()
            if row is None:
                return None
            fetched = row.fetched_at
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=timezone.utc)
            if fetched < cutoff:
                return None  # stale; caller will refetch and overwrite
            return [_to_comp(d) for d in json.loads(row.payload_json)]
    except Exception:  # noqa: BLE001
        logger.exception("comp cache read failed for %r", key)
        return None


def put(query: str, *, graded: bool, marketplace: str, comps: list[SoldComp]) -> None:
    """Store (or refresh) the pooled comps for a card identity."""
    if get_settings().price_cache_ttl_days <= 0:
        return
    from app.models import PriceCache

    key = _key(query, graded, marketplace)
    payload = json.dumps([asdict(c) for c in comps])
    now = datetime.now(timezone.utc)
    try:
        with SessionLocal() as db:
            row = db.query(PriceCache).filter(PriceCache.query_key == key).one_or_none()
            if row is None:
                db.add(PriceCache(query_key=key, payload_json=payload, fetched_at=now))
            else:
                row.payload_json = payload
                row.fetched_at = now
            db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("comp cache write failed for %r", key)
