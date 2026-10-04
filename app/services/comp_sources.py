"""Aggregate real price comps from every configured source. No fabricated data.

SOLD prices:
  - eBay Marketplace Insights API   (insights.py)        — official, gated
  - SportsCardsPro / PriceCharting  (pricecharting.py)   — paid token; aggregate
      price + full grade-tier breakdown + (opt-in) scraped individual sales
  - 130point sold search            (point130.py)        — incl. hidden best offers
  - Headless-browser eBay scrape    (browser_scrape.py)  — best-effort, ToS-gray
ACTIVE asking prices:
  - eBay Browse API                 (browse.py)          — free keyset

`collect_comps` returns a CompResult: the comps, honest user-facing notes, and
one SourceStatus per source asked (ok / empty / a failure state with its
message). `gather_comps` keeps the older (comps, notes) shape.

Every fetch also updates the app-level SOURCE HEALTH registry (last status,
last error, last success per source), served by GET /api/sources/health so the
UI can show a banner such as "SportsCardsPro token expired". It lives in memory
and is persisted through the caller's session (`persist_health`) so it survives
a restart.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.config import get_settings
from app.services import comp_cache, point130, pricecharting
from app.services.ebay import browse, browser_scrape, insights
from app.services.ebay.base import SoldComp
from app.services.ebay.scrape import fetch_sold_comps as scrape_sold

logger = logging.getLogger("comp_sources")

# --- Source status ----------------------------------------------------------------

OK = "ok"
EMPTY = "empty"
ERROR = "error"
AUTH_EXPIRED = "auth_expired"
UNAUTHORIZED = "unauthorized"
BLOCKED = "blocked"
QUOTA = "quota"
PROBLEM_STATES = frozenset({ERROR, AUTH_EXPIRED, UNAUTHORIZED, BLOCKED, QUOTA})

SOURCE_LABELS = {
    "insights": "eBay sold (Insights)",
    "sportscardspro": "SportsCardsPro",
    "sportscardspro_sales": "SportsCardsPro recent sales",
    "130point": "130point",
    "ebay_browser_scrape": "eBay sold (browser scrape)",
    "ebay_browse": "eBay asking prices (Browse)",
    "ebay_scrape": "eBay sold (plain scrape)",
}

# Every card-facing note about a failing source starts with this, so pricing
# can keep it (and lead with it) when other steps rewrite the review reason.
SOURCE_PROBLEM_PREFIX = "Price source problem: "

_HEALTH_DOC_KEY = "__source_health__"


@dataclass
class SourceStatus:
    source: str
    state: str
    message: str | None = None
    count: int = 0

    @property
    def label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source)

    @property
    def problem(self) -> bool:
        return self.state in PROBLEM_STATES

    def note(self) -> str:
        """Card-facing text for a failing source (one "; "-free segment)."""
        msg = (self.message or self.state).replace(";", ",").strip()
        if not msg.lower().startswith(self.label.lower().split(" ")[0].lower()):
            msg = f"{self.label}: {msg}"
        return SOURCE_PROBLEM_PREFIX + msg


@dataclass
class CompResult:
    comps: list[SoldComp] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    statuses: list[SourceStatus] = field(default_factory=list)
    from_cache: bool = False

    @property
    def problems(self) -> list[SourceStatus]:
        return [s for s in self.statuses if s.problem]


# --- App-level source health ---------------------------------------------------------

_health: dict[str, dict] = {}
_health_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_status(status: SourceStatus) -> None:
    """Fold one fetch outcome into the app-level health registry."""
    now = _now()
    with _health_lock:
        entry = _health.setdefault(status.source, {
            "source": status.source,
            "label": status.label,
            "last_success_at": None,
            "last_error": None,
            "last_error_at": None,
        })
        entry.update({
            "state": status.state,
            "ok": not status.problem,
            "message": status.message,
            "count": status.count,
            "last_checked_at": now,
        })
        if status.problem:
            entry["last_error"] = status.message or status.state
            entry["last_error_at"] = now
        else:
            entry["last_success_at"] = now


def source_health(db=None) -> dict:
    """Snapshot for GET /api/sources/health.

    {"sources": [entry...], "problems": [entry...], "banner": str | None}
    where entry = {source, label, state, ok, message, count, last_checked_at,
    last_success_at, last_error, last_error_at}. Sources not yet asked since
    the app started are filled from the persisted copy when `db` is given.
    """
    with _health_lock:
        current = {k: dict(v) for k, v in _health.items()}
    if db is not None:
        saved = comp_cache.get_document(_HEALTH_DOC_KEY, db) or {}
        for name, entry in (saved.get("sources") or {}).items():
            current.setdefault(name, dict(entry))
    sources = sorted(current.values(), key=lambda e: e.get("label") or e["source"])
    problems = [e for e in sources if not e.get("ok", True)]
    banner = None
    if problems:
        banner = "; ".join(
            f"{e.get('label') or e['source']}: {e.get('message') or e.get('state')}"
            for e in problems
        )
    return {"sources": sources, "problems": problems, "banner": banner}


def persist_health(db) -> None:
    """Save the health registry through the caller's session (caller commits)."""
    with _health_lock:
        snapshot = {k: dict(v) for k, v in _health.items()}
    if snapshot:
        comp_cache.put_document(_HEALTH_DOC_KEY, {"sources": snapshot}, db)


def reset_health() -> None:
    """Forget in-memory health (tests)."""
    with _health_lock:
        _health.clear()


# --- Fetching ------------------------------------------------------------------------


def _run(source: str, fetch, result: CompResult) -> list[SoldComp]:
    """Call one source, turning its outcome into a SourceStatus. A failing
    source never fails the card; it is reported instead."""
    try:
        got = list(fetch() or [])
    except Exception as exc:  # noqa: BLE001 — every source failure is reported
        state = getattr(exc, "state", None) or ERROR
        if state not in PROBLEM_STATES:
            state = ERROR
        message = pricecharting.redact_token(str(exc)) or type(exc).__name__
        if state == ERROR and not getattr(exc, "state", None):
            logger.exception("%s failed", SOURCE_LABELS.get(source, source))
        status = SourceStatus(source, state, message)
        got = []
    else:
        status = SourceStatus(source, OK if got else EMPTY, None, len(got))
    result.statuses.append(status)
    record_status(status)
    return got


def collect_comps(
    query: str,
    *,
    graded: bool = False,
    use_cache: bool = True,
    refresh: bool = False,
    require_parallel: str | None = None,
    require_number: str | None = None,
    require_player: str | None = None,
    db=None,
) -> CompResult:
    """Fetch comps from every configured source, with a status per source.

    `db` is the caller's session, used for the comp cache so a cache write can
    never deadlock against the caller's open transaction.
    """
    s = get_settings()
    result = CompResult()

    # Persistent cache: reuse a recent pooled result for this card identity
    # instead of re-hitting the price APIs (see comp_cache for lifetimes).
    # `refresh` forces a live re-fetch (skips the read) but still updates/merges
    # the cache below — used by the "Refresh prices" action.
    if use_cache and not refresh:
        cached = comp_cache.get(
            query, graded=graded, marketplace=s.ebay_marketplace_id, db=db
        )
        if cached is not None:
            result.comps = cached
            result.notes = ["prices reused from cache (no API calls)"]
            result.from_cache = True
            return result

    comps = result.comps
    has_ebay_creds = bool(s.ebay_client_id and s.ebay_client_secret)
    scp_kw = dict(
        require_parallel=require_parallel,
        require_number=require_number,
        require_player=require_player,
    )

    # --- SOLD: eBay Marketplace Insights ---
    # When it is switched off on purpose there is nothing to report.
    if insights.is_enabled():
        comps += _run("insights", lambda: insights.fetch_sold_comps(query, graded=graded), result)

    # --- SOLD: PriceCharting / SportsCardsPro ---
    if pricecharting.has_token():
        def scp() -> list[SoldComp]:
            # Aggregate price for the target grade (drives the estimate), plus
            # the full graded-tier breakdown (PSA 9/10, BGS, SGC, CGC) as
            # informational comps filed as "graded". Tiers only on the raw pass
            # to avoid duplication.
            got = pricecharting.fetch_comps(query, graded=graded, **scp_kw)
            if not graded:
                got += pricecharting.fetch_grade_tiers(query, **scp_kw)
            return got

        comps += _run("sportscardspro", scp, result)
        scp_ok = not result.statuses[-1].problem
        # Individual dated sales scraped from the product page (opt-in). These
        # are the product's sales tables, so they belong to the raw pass only.
        # Skipped when the catalogue itself just failed (same token/product).
        if not graded and scp_ok and s.sportscardspro_sales_enabled:
            comps += _run(
                "sportscardspro_sales",
                lambda: pricecharting.fetch_individual_sales(query, **scp_kw),
                result,
            )

    # --- SOLD: 130point (captures hidden best-offer-accepted prices) ---
    if point130.is_enabled():
        comps += _run("130point", lambda: point130.fetch_sold_comps(query, graded=graded), result)

    # --- SOLD: headless-browser eBay scrape (best-effort) ---
    if browser_scrape.is_enabled():
        comps += _run(
            "ebay_browser_scrape",
            lambda: browser_scrape.fetch_sold_comps(query, graded=graded),
            result,
        )

    # --- ACTIVE: eBay Browse ---
    if has_ebay_creds:
        comps += _run("ebay_browse", lambda: browse.fetch_active_comps(query, graded=graded), result)

    if not comps:
        # Last resort: plain scrape (usually 403). Its status is recorded for
        # the health view but never shown on a card: it is expected to fail.
        scraped = list(scrape_sold(query) or [])
        status = SourceStatus("ebay_scrape", OK if scraped else EMPTY, None, len(scraped))
        record_status(status)
        comps += scraped
        if not scraped and not (has_ebay_creds or pricecharting.has_token()):
            result.notes.append(
                "No price source configured. Add EBAY_CLIENT_ID/SECRET, a "
                "PRICECHARTING_TOKEN, or enable EBAY_BROWSER_SCRAPE_ENABLED."
            )

    # Failing sources lead the notes, in a form pricing can recognise.
    result.notes[:0] = [st.note() for st in result.problems]

    # Only cache a complete, real result. Never lock in an empty result, and
    # never one where a source failed (an expired token must not be cached as
    # "no SportsCardsPro price" for the next week).
    if use_cache and comps and not result.problems:
        comp_cache.put(
            query, graded=graded, marketplace=s.ebay_marketplace_id, comps=comps, db=db
        )

    return result


def gather_comps(
    query: str,
    *,
    graded: bool = False,
    use_cache: bool = True,
    refresh: bool = False,
    require_parallel: str | None = None,
    require_number: str | None = None,
    require_player: str | None = None,
    db=None,
) -> tuple[list[SoldComp], list[str]]:
    """Backward-compatible (comps, notes) form of `collect_comps`."""
    result = collect_comps(
        query, graded=graded, use_cache=use_cache, refresh=refresh,
        require_parallel=require_parallel, require_number=require_number,
        require_player=require_player, db=db,
    )
    return result.comps, result.notes
