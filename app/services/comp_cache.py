"""Persistent cache of pooled comps, keyed by card identity.

Pricing the same card identity (across uploads and app restarts) reuses a stored
result instead of re-querying eBay Browse / PriceCharting / etc. Stores the
pooled SoldComp list as JSON in the price_cache table.

Lifetimes (see settings):
  - an entry of dated SOLD comps is reused for PRICE_CACHE_TTL_DAYS (30);
  - an entry that also holds fast-moving data (current asking prices, or a
    SportsCardsPro market average) is reused only for
    PRICE_CACHE_ACTIVE_TTL_DAYS (7).
Empty results are never stored (the caller also skips storing a result where a
source failed, so an expired token cannot lock in an empty price).

Sessions: pass the caller's `db` session. The upload request holds SQLite's
write lock for its whole transaction, so a cache write on a SECOND connection
waits on that lock and fails with "database is locked". With `db`, reads and
writes run inside the caller's transaction (a SAVEPOINT isolates a failed write
so it cannot poison the caller), and the caller commits. Without `db` the old
behaviour (own short session + commit) is kept for other callers. Failures are
logged at ERROR, never swallowed silently, and never fail a price.

Refreshes are INCREMENTAL for dated sold comps: a refetch is merged into the
stored set (union + dedupe) rather than overwriting it, so real sale history
accumulates even after sales age out of eBay's/SportsCardsPro's lookback
windows. Snapshots are NOT accumulated, because keeping stale copies would be
wrong: ACTIVE asking prices (a delisted item shouldn't linger), UNDATED sold
comps, and market AVERAGES (SportsCardsPro's aggregate price, which carries its
fetch date but is a moving snapshot rather than a sale). For those we keep only
the latest fetch. Dated sold comps older than
settings.price_history_retention_days are pruned.
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

# Source names whose comps are market averages, not individual sales.
AGGREGATE_SOURCES = frozenset({"sportscardspro"})


def is_aggregate(c: SoldComp) -> bool:
    """A market average (SportsCardsPro's per-grade price), not a sale."""
    return (c.source or "").strip().lower() in AGGREGATE_SOURCES


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


def _is_history(c: SoldComp) -> bool:
    """A dated individual sale: the only kind of comp that accumulates."""
    return c.kind == "sold" and not is_aggregate(c) and _sold_date_obj(c) is not None


def merge_comps(
    old: list[SoldComp], new: list[SoldComp], *, retention_days: int
) -> list[SoldComp]:
    """Accumulate dated sold history; keep snapshot comps from `new` only.

    - dated individual sales: union(old, new) deduped by identity, pruned to
      the last `retention_days`
    - active comps, undated sold comps and market averages: taken from `new`
      only (they go stale)
    """
    merged: dict[tuple, SoldComp] = {}
    # Accumulate dated sold comps from the existing set first, then let `new`
    # overwrite same-identity entries with the fresher copy.
    for c in old:
        if _is_history(c):
            merged[_comp_key(c)] = c
    for c in new:
        if _is_history(c):
            merged[_comp_key(c)] = c

    if retention_days > 0:
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=retention_days)
        history = [c for c in merged.values() if (_sold_date_obj(c) or cutoff) >= cutoff]
    else:
        history = list(merged.values())

    snapshot = [c for c in new if not _is_history(c)]
    return history + snapshot


def _key(query: str, graded: bool, marketplace: str) -> str:
    norm = re.sub(r"\s+", " ", (query or "").strip().lower())
    return f"{marketplace}|{'graded' if graded else 'raw'}|{norm}"


def _to_comp(d: dict) -> SoldComp:
    # Tolerate schema drift: drop unknown keys.
    return SoldComp(**{k: v for k, v in d.items() if k in _COMP_FIELDS})


def _entry_ttl_days(comps: list[SoldComp], settings) -> int:
    """Lifetime of a cached entry: the sold TTL, shortened to the active TTL
    when the entry holds asking prices or market averages."""
    ttl = settings.price_cache_ttl_days
    if any(not _is_history(c) for c in comps):
        ttl = min(ttl, settings.price_cache_active_ttl_days)
    return ttl


def _read(db, key: str, settings) -> list[SoldComp] | None:
    from app.models import PriceCache

    row = db.query(PriceCache).filter(PriceCache.query_key == key).one_or_none()
    if row is None:
        return None
    comps = [_to_comp(d) for d in json.loads(row.payload_json)]
    fetched = row.fetched_at
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - fetched
    if age > timedelta(days=_entry_ttl_days(comps, settings)):
        return None  # stale; caller will refetch and merge
    return comps


def get(
    query: str, *, graded: bool, marketplace: str, db=None
) -> list[SoldComp] | None:
    """Return cached comps if a fresh entry exists, else None."""
    s = get_settings()
    if s.price_cache_ttl_days <= 0:
        return None
    key = _key(query, graded, marketplace)
    try:
        if db is not None:
            return _read(db, key, s)
        with SessionLocal() as own:
            return _read(own, key, s)
    except Exception:  # noqa: BLE001
        logger.exception("comp cache read failed for %r", key)
        return None


def _write(db, key: str, comps: list[SoldComp], settings) -> None:
    from app.models import PriceCache

    now = datetime.now(timezone.utc)
    row = db.query(PriceCache).filter(PriceCache.query_key == key).one_or_none()
    if row is None:
        merged = merge_comps([], comps, retention_days=settings.price_history_retention_days)
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
        merged = merge_comps(
            existing, comps, retention_days=settings.price_history_retention_days
        )
        row.payload_json = json.dumps([asdict(c) for c in merged])
        row.fetched_at = now
    db.flush()


def put(
    query: str, *, graded: bool, marketplace: str, comps: list[SoldComp], db=None
) -> None:
    """Store the pooled comps for a card identity, merging into any prior set.

    Dated sold comps accumulate (union/dedupe/prune); snapshot comps are
    replaced with this fetch. See `merge_comps` and the module docstring.
    With `db`, the write joins the caller's transaction (the caller commits).
    """
    s = get_settings()
    if s.price_cache_ttl_days <= 0 or not comps:
        return
    key = _key(query, graded, marketplace)
    try:
        if db is not None:
            # A SAVEPOINT: a failed cache write rolls back only itself.
            with db.begin_nested():
                _write(db, key, comps, s)
            return
        with SessionLocal() as own:
            _write(own, key, comps, s)
            own.commit()
    except Exception:  # noqa: BLE001
        logger.error(
            "comp cache write FAILED for %r; prices were not cached", key, exc_info=True
        )


# --- Small JSON documents (e.g. source health) -----------------------------------
# Stored as a price_cache row under a reserved key so no schema change is needed.
# Reserved keys never collide with comp keys ("<marketplace>|raw|<query>").


def put_document(key: str, payload: dict, db) -> None:
    """Upsert a JSON document under a reserved key in the caller's session."""
    from app.models import PriceCache

    try:
        with db.begin_nested():
            row = db.query(PriceCache).filter(PriceCache.query_key == key).one_or_none()
            text = json.dumps(payload)
            now = datetime.now(timezone.utc)
            if row is None:
                db.add(PriceCache(query_key=key, payload_json=text, fetched_at=now))
            else:
                row.payload_json = text
                row.fetched_at = now
            db.flush()
    except Exception:  # noqa: BLE001
        logger.error("could not store %r", key, exc_info=True)


def get_document(key: str, db) -> dict | None:
    from app.models import PriceCache

    try:
        row = db.query(PriceCache).filter(PriceCache.query_key == key).one_or_none()
        return json.loads(row.payload_json) if row else None
    except Exception:  # noqa: BLE001
        logger.error("could not read %r", key, exc_info=True)
        return None
