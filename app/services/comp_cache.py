"""Persistent cache of pooled comps, keyed by card identity.

Pricing the same card identity (across uploads and app restarts) reuses a stored
result within settings.price_cache_ttl_days instead of re-querying eBay Browse /
PriceCharting / etc. Stores the pooled SoldComp list as JSON in the price_cache
table. Best-effort: any DB hiccup degrades to "no cache" rather than failing a
price.

Refreshes are INCREMENTAL for dated sold comps: a refetch is merged into the
stored set (union + dedupe) rather than overwriting it, so real sale history
accumulates even after sales age out of eBay's/SportsCardsPro's lookback
windows. Two kinds of comp are NOT accumulated, because keeping stale copies
would be wrong: ACTIVE asking prices (a delisted item shouldn't linger) and
UNDATED sold comps (e.g. SportsCardsPro's aggregate market price, a moving
snapshot rather than a dated event). For those we keep only the latest fetch.
Dated sold comps older than settings.price_history_retention_days are pruned.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, fields
from datetime import date, datetime, timedelta, timezone

from app.config import get_settings
from app.db import SessionLocal
from app.services.ebay.base import SoldComp

logger = logging.getLogger("comp_cache")

_COMP_FIELDS = {f.name for f in fields(SoldComp)}


def _comp_key(c: SoldComp) -> tuple:
    """Stable identity for dedupe.

    The URL alone is not an identity: SportsCardsPro gives every premium-locked
    sale the same product-page link, so keying on it collapses a whole sales
    history into one row. Price + date separate those while still deduping a
    genuine refetch of the same sale.
    """
    if c.listing_url:
        return ("url", c.listing_url, c.sold_price, c.sold_date)
    return ("shape", c.source, c.title, c.sold_price, c.sold_date)


def _sold_date_obj(c: SoldComp) -> date | None:
    """Parse an ISO sold_date to a date; None if missing/unparseable."""
    if not c.sold_date:
        return None
    try:
        return date.fromisoformat(c.sold_date[:10])
    except (ValueError, TypeError):
        return None


def merge_comps(
    old: list[SoldComp], new: list[SoldComp], *, retention_days: int
) -> list[SoldComp]:
    """Accumulate dated sold history; keep active/undated comps from `new` only.

    - dated sold comps: union(old, new) deduped by identity, pruned to the last
      `retention_days` (undateable ones are kept — we don't drop what we can't date)
    - active comps and undated sold comps: taken from `new` only (they go stale)
    """
    def is_dated_sold(c: SoldComp) -> bool:
        return c.kind == "sold" and _sold_date_obj(c) is not None

    merged: dict[tuple, SoldComp] = {}
    # Accumulate dated sold comps from the existing set first, then let `new`
    # overwrite same-identity entries with the fresher copy.
    for c in old:
        if is_dated_sold(c):
            merged[_comp_key(c)] = c
    for c in new:
        if is_dated_sold(c):
            merged[_comp_key(c)] = c

    if retention_days > 0:
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=retention_days)
        history = [c for c in merged.values() if (_sold_date_obj(c) or cutoff) >= cutoff]
    else:
        history = list(merged.values())

    # Active + undated-sold: only the latest fetch counts.
    snapshot = [c for c in new if not is_dated_sold(c)]
    return history + snapshot


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
    """Store the pooled comps for a card identity, merging into any prior set.

    Dated sold comps accumulate (union/dedupe/prune); active and undated comps
    are replaced with this fetch. See `merge_comps` and the module docstring.
    """
    s = get_settings()
    if s.price_cache_ttl_days <= 0:
        return
    from app.models import PriceCache

    key = _key(query, graded, marketplace)
    now = datetime.now(timezone.utc)
    try:
        with SessionLocal() as db:
            row = db.query(PriceCache).filter(PriceCache.query_key == key).one_or_none()
            if row is None:
                merged = merge_comps([], comps, retention_days=s.price_history_retention_days)
                db.add(
                    PriceCache(
                        query_key=key,
                        payload_json=json.dumps([asdict(c) for c in merged]),
                        fetched_at=now,
                    )
                )
            else:
                try:
                    existing = [_to_comp(d) for d in json.loads(row.payload_json)]
                except Exception:  # noqa: BLE001 — corrupt payload: start fresh
                    existing = []
                merged = merge_comps(existing, comps, retention_days=s.price_history_retention_days)
                row.payload_json = json.dumps([asdict(c) for c in merged])
                row.fetched_at = now
            db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("comp cache write failed for %r", key)
