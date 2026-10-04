"""Pricing orchestrator + safeguard gating.

Accuracy first. Prices come ONLY from real eBay data:
  - SOLD prices via Marketplace Insights (when enabled/approved), and
  - CURRENT ASKING prices via the Browse API.
A price is NEVER invented. The estimate prefers real SOLD data and falls back to
ACTIVE asking prices, always labeling which basis was used. If neither is
available the card is flagged for manual review.

Writes Comp rows and mutates the Card in place; the caller commits.
"""
from __future__ import annotations

import logging
import re
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    STATUS_BELOW_THRESHOLD,
    STATUS_DELETED,
    STATUS_LIST_FAILED,
    STATUS_LISTED,
    STATUS_NEEDS_REVIEW,
    STATUS_PREVIEW,
    STATUS_PRICED,
    Card,
    Comp,
)
from app.services import comp_cache, comp_sources, ref_image, websearch
from app.services.ebay.base import SoldComp
from app.services.matching import partition

logger = logging.getLogger("pricing")

CompFetcher = Callable[..., list[SoldComp]]


def build_query(card: Card) -> str:
    # serial_number is excluded — it over-narrows the search and zeroes comps.
    number = str(card.card_number).lstrip("#").strip() if card.card_number else ""
    parts = [
        str(card.year or ""),
        card.set_brand or "",
        card.player or "",
        f"#{number}" if number else "",
        card.parallel or "",
    ]
    return " ".join(p for p in parts if p).strip()




def _has_core_identity(card: Card) -> bool:
    has_player = bool(card.player and card.player.strip())
    has_year_or_set = bool(
        (card.year and card.year.strip()) or (card.set_brand and card.set_brand.strip())
    )
    return has_player and has_year_or_set


def _sale_date(sold_date: str | None) -> date | None:
    """A comp's sale date, or None when it is missing or unparseable."""
    if not sold_date:
        return None
    try:
        return datetime.fromisoformat(sold_date[:10]).date()
    except ValueError:
        return None


def _within_recency(sold_date: str | None, cutoff: date) -> bool:
    """Not older than the recency window. An undated comp is not "stale" (it
    is kept), but it is never counted as a RECENT sale; see _sold_pool."""
    d = _sale_date(sold_date)
    return d is None or d >= cutoff


def _trim_outliers(prices: list[float]) -> list[float]:
    if len(prices) < 4:
        return prices
    q = statistics.quantiles(prices, n=4)
    q1, q3 = q[0], q[2]
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    return [p for p in prices if lo <= p <= hi] or prices


def _estimate(prices: list[float]) -> tuple[float | None, int]:
    """(median of trimmed prices, count kept) or (None, 0)."""
    if not prices:
        return None, 0
    kept = _trim_outliers(prices)
    return round(statistics.median(kept), 2), len(kept)


@dataclass
class _SoldPool:
    """The sold prices behind an estimate, and what kind they are."""

    prices: list[float] = field(default_factory=list)
    n_sales: int = 0      # individual sales (after cross-source dedupe)
    n_averages: int = 0   # market averages (SportsCardsPro), not sales
    sources: list[str] = field(default_factory=list)
    undated: bool = False  # no dated recent sale; fell back to undated sales


def _sale_key(c: SoldComp) -> tuple:
    return (
        round(c.sold_price or 0.0, 2),
        c.sold_date,
        " ".join(re.findall(r"[a-z0-9]+", (c.title or "").lower()))[:80],
    )


def _dedupe_across_sources(comps: list[SoldComp], primary: str) -> list[SoldComp]:
    """Drop the same sale reported by two sources (e.g. eBay Insights and
    130point both list one eBay sale). Same-source repeats are kept: one
    seller can sell several copies at one price on one day. For each sale,
    the copies from the source reporting it most often survive, preferring
    the configured primary source on a tie."""
    key_primary = (primary or "").strip().lower()
    groups: dict[tuple, dict[str, list[SoldComp]]] = {}
    for c in comps:
        groups.setdefault(_sale_key(c), {}).setdefault(c.source or "", []).append(c)
    kept: list[SoldComp] = []
    for by_source in groups.values():
        best = max(
            by_source.items(),
            key=lambda kv: (
                len(kv[1]),
                bool(key_primary) and kv[0].lower().startswith(key_primary),
            ),
        )
        kept.extend(best[1])
    return kept


def _sold_pool(comps: list[SoldComp], cutoff: date, primary: str) -> _SoldPool:
    """Pool every SOLD source for one estimate.

    - dated individual sales inside the recency window, deduped across sources;
    - market averages (SportsCardsPro), dated with their fetch day;
    - undated individual sales only when no dated recent sale exists, and then
      flagged, because their age is unknown.
    The primary source is a dedupe tie-break, never a filter: one SportsCardsPro
    average no longer silences every real sale.
    """
    recent: list[SoldComp] = []
    undated: list[SoldComp] = []
    averages: list[SoldComp] = []
    for c in comps:
        if not c.sold_price:
            continue
        d = _sale_date(c.sold_date)
        if d is not None and d < cutoff:
            continue  # stale
        if comp_cache.is_aggregate(c):
            averages.append(c)
        elif d is None:
            undated.append(c)
        else:
            recent.append(c)
    pool = _SoldPool()
    sales = _dedupe_across_sources(recent, primary)
    if not sales and undated:
        sales = undated
        pool.undated = True
    chosen = sales + averages
    pool.prices = [c.sold_price for c in chosen]
    pool.n_sales = len(sales)
    pool.n_averages = len(averages)
    pool.sources = sorted({c.source for c in chosen if c.source})
    return pool


def _describe_sold(pool: _SoldPool, est: float, kept: int) -> str:
    parts = []
    if pool.n_sales:
        kind = "undated SOLD sale(s) (sale dates unknown)" if pool.undated else "recent SOLD sale(s)"
        parts.append(f"{pool.n_sales} {kind}")
    if pool.n_averages:
        parts.append(f"{pool.n_averages} SportsCardsPro market average(s), not individual sales")
    text = f"based on {' + '.join(parts)} from {', '.join(pool.sources) or 'mixed'} (median ${est})"
    if kept < len(pool.prices):
        text += f"; {len(pool.prices) - kept} outlier(s) trimmed"
    return text


def _split_reason(reason: str | None) -> tuple[list[str], list[str]]:
    """(source-problem segments, other segments) of a "; "-joined reason.

    Source problems (a rejected token, a blocked site) explain WHY a card has
    no price, so every step that rewrites the reason keeps them, first."""
    parts = [p.strip() for p in (reason or "").split("; ") if p.strip()]
    problems = [p for p in parts if p.startswith(comp_sources.SOURCE_PROBLEM_PREFIX)]
    return problems, [p for p in parts if p not in problems]


def _join_reason(*groups: list[str]) -> str | None:
    seen: list[str] = []
    for group in groups:
        for part in group:
            if part and part not in seen:
                seen.append(part)
    return "; ".join(seen) or None


def _gate(card: Card, settings) -> str | None:
    """Safeguard checks run before a card may be priced/promoted. Returns a
    review reason if the card should be flagged, else None."""
    if (card.confidence or 0.0) < settings.confidence_threshold:
        return "low identification confidence"
    if not _has_core_identity(card):
        return "incomplete identification"
    return None


def price_card(
    card: Card, db: Session, comp_fetcher: CompFetcher | None = None, *,
    refresh: bool = False, commit_after_fetch: bool = False,
) -> Card:
    settings = get_settings()

    # --- Safeguards before pricing ---
    reason = _gate(card, settings)
    if reason:
        return _flag_review(card, reason)

    _compute_pricing(
        card, db, comp_fetcher, refresh=refresh, commit_after_fetch=commit_after_fetch
    )
    return _route_status(card, settings)


def preview_card(
    card: Card, db: Session, comp_fetcher: CompFetcher | None = None, *,
    refresh: bool = False, commit_after_fetch: bool = False,
) -> Card:
    """Price a freshly detected card for *review* without promoting it.

    Runs the full comp gathering (so the marketplace reference photo and a
    tentative estimate appear even for low-confidence cards — the very ones the
    user needs to verify), but leaves the card in STATUS_PREVIEW. It enters the
    library only when the user explicitly promotes it via finalize_card.
    """
    if _has_core_identity(card):
        _compute_pricing(
            card, db, comp_fetcher, refresh=refresh, commit_after_fetch=commit_after_fetch
        )
        if card.estimated_price is None and not card.review_reason:
            # Identified, but no marketplace match was found for the query.
            card.review_reason = "no marketplace match for this identification"
    else:
        # Can't query without a player + (year or set) — tell the user why.
        card.review_reason = "incomplete identification: can't price, edit it manually"
    card.status = STATUS_PREVIEW
    return card


def reprice_after_pairing(
    card: Card, db: Session, *, commit_after_fetch: bool = False
) -> Card:
    """Re-price a front that just absorbed a back's sharper identity.

    Pairing happens at any point in a card's life, including long after it was
    promoted (photographing fronts first and backs later is the normal
    workflow), so this must never move a card backwards: a preview stays a
    preview, a library card is re-priced and re-routed but stays in the library,
    and a card with a live eBay listing is left alone — its price is the one it
    is listed at. Caller commits.
    """
    if card.status in (STATUS_LISTED, STATUS_LIST_FAILED):
        return card
    if card.status == STATUS_DELETED:
        return card
    if card.status == STATUS_PREVIEW:
        return preview_card(card, db, commit_after_fetch=commit_after_fetch)
    if _has_core_identity(card):
        _compute_pricing(card, db, commit_after_fetch=commit_after_fetch)
    return _route_status(card, get_settings())


def price_from_url(card: Card, db: Session, url: str) -> bool:
    """Price a card from a user-pasted SportsCardsPro product URL, for when the
    automatic search matched the wrong card (or nothing). Pins the card's
    identity to that product, prices from its data, and sets its cover image.
    Returns True if the page yielded usable price data. Caller commits."""
    from app.services import pricecharting

    raw, graded_tiers, image, ident = pricecharting.data_from_url(url)
    if not raw and not graded_tiers:
        return False

    # Pin the card to the chosen product so it's labelled correctly and future
    # re-prices match.
    if ident:
        for k in ("player", "year", "set_brand", "card_number"):
            if ident.get(k):
                setattr(card, k, ident[k])

    # Drop the previous comps, then price from the pasted product's data only
    # (injected fetcher = no re-search).
    for comp in list(card.comps):
        db.delete(comp)
    db.flush()

    def fetcher(_q: str, graded: bool = False) -> list[SoldComp]:
        return graded_tiers if graded else raw

    _compute_pricing(card, db, fetcher)
    if image:
        if card.id is None:
            db.flush()
        card.reference_image_url = ref_image.localize(card.id, image)

    settings = get_settings()
    if card.status == STATUS_PREVIEW:
        card.review_reason = None if card.estimated_price is not None else card.review_reason
    else:
        _route_status(card, settings)
    return True


def finalize_card(card: Card, settings) -> Card:
    """Promote a previewed card into the library, applying the same safeguards
    and status routing as a normal price. Reuses the estimate already computed at
    preview time — no comp re-fetch. A source failure recorded at preview time
    (e.g. an expired SportsCardsPro token) stays at the front of the reason."""
    reason = _gate(card, settings)
    if reason:
        problems, _ = _split_reason(card.review_reason)
        return _flag_review(card, _join_reason(problems, [reason]))
    return _route_status(card, settings)


def _compute_pricing(
    card: Card, db: Session, comp_fetcher: CompFetcher | None = None, *,
    refresh: bool = False, commit_after_fetch: bool = False,
) -> None:
    """Fetch comps, compute estimates, set the reference image, and write Comp
    rows. Mutates the card in place; does NOT gate or route status.
    `refresh` forces a live re-fetch instead of using cached comps.

    Network first, writes last: every comp fetch (raw, and graded for a PSA 10
    candidate) runs before any Comp row is touched. `commit_after_fetch`
    (background jobs) then commits the comp-cache writes the fetch made, so the
    SQLite write lock is not held across the reference-photo download either."""
    settings = get_settings()
    notes: list[str] = []

    query = build_query(card)
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=settings.comp_recency_days)

    # Comp fetch: injected fetcher (tests) returns a list; default returns notes too.
    if comp_fetcher is not None:
        raw_comps = comp_fetcher(query)

        def fetch_graded() -> list[SoldComp]:
            return comp_fetcher(query, graded=True)
    else:
        # The caller's session goes along so the comp cache writes inside this
        # transaction instead of deadlocking against it.
        raw_comps, notes = comp_sources.gather_comps(
            query, refresh=refresh,
            require_parallel=card.parallel, require_number=card.card_number,
            require_player=card.player, db=db,
        )
        notes = list(notes)
        comp_sources.persist_health(db)

        def fetch_graded() -> list[SoldComp]:
            return comp_sources.gather_comps(
                query, graded=True, refresh=refresh,
                require_parallel=card.parallel, require_number=card.card_number,
                require_player=card.player, db=db,
            )[0]

    graded_comps = fetch_graded() if card.psa10_candidate else []
    if commit_after_fetch:
        db.commit()

    # Comp rows describe the CURRENT estimate, so a re-price replaces them.
    # Leaving this to callers meant several re-price paths piled up duplicates.
    for stale in list(card.comps):
        db.delete(stale)
    db.flush()

    scored = partition(card, raw_comps)
    card.excluded_count = sum(1 for s in scored if s.match_type == "excluded")

    sold_exact: list[SoldComp] = []
    active_prices: list[float] = []
    matched_sources: list[str] = []
    ref_candidates: list[tuple[str, str, str]] = []  # (match_type, source, thumb)
    for s in scored:
        if s.match_type == "excluded":
            continue
        db.add(_comp_row(card, s.comp, s.match_type, s.match_reason))
        if s.comp.source and s.comp.source not in matched_sources:
            matched_sources.append(s.comp.source)
        if s.comp.thumbnail_url:
            ref_candidates.append((s.match_type, s.comp.source or "", s.comp.thumbnail_url))
        if s.match_type == "exact" and s.comp.sold_price:
            if s.comp.kind == "sold":
                sold_exact.append(s.comp)  # recency applied in _sold_pool
            elif _within_recency(s.comp.sold_date, cutoff):
                active_prices.append(s.comp.sold_price)

    # Always reflect THIS run's sources (clear stale ones when nothing matched).
    card.price_sources = ", ".join(matched_sources) if matched_sources else None
    # Reference photo: prefer SportsCardsPro's clean catalogue scan of the exact
    # matched card; fall back to an exact eBay listing photo, then a wider search.
    ref_url = None
    if comp_fetcher is None and "sportscardspro" in matched_sources:
        ref_url = _scp_reference_image(card)
    if not ref_url:
        ref_url = _pick_reference_image(ref_candidates)
    if not ref_url and comp_fetcher is None:
        ref_url = _scp_reference_image(card)
    if ref_url:
        if card.id is None:
            db.flush()  # need the card id to name the saved file
        ref_url = ref_image.localize(card.id, ref_url)
    card.reference_image_url = ref_url

    # Pool every sold source (deduping one sale reported twice); the primary
    # source is only a dedupe tie-break.
    pool = _sold_pool(sold_exact, cutoff, settings.primary_sold_source)
    sold_est, sold_kept = _estimate(pool.prices)
    active_est, active_n = _estimate(active_prices)
    card.sold_estimate = sold_est
    # Top of the raw (ungraded) sold range, from the outlier-trimmed set so one
    # mislabelled sale can't set it.
    card.sold_max_estimate = (
        round(max(_trim_outliers(pool.prices)), 2) if pool.prices else None
    )
    card.active_estimate = active_est

    # Start from a clean slate so a re-price that finds nothing clears any stale
    # estimate from a previous run (rather than silently keeping a wrong price).
    card.estimated_price = None
    card.price_basis = None
    card.price_source = None
    card.derivation = None

    # Prefer real SOLD data; fall back to ACTIVE asking prices.
    if sold_est is not None:
        card.estimated_price = sold_est
        card.price_basis = "sold"
        card.price_source = "ebay_sold"
        card.derivation = _describe_sold(pool, sold_est, sold_kept) + (
            f"; current asking median ${active_est}" if active_est else ""
        )
        # Trust rests on individual sales. A market average alone, or a couple
        # of sales, is said plainly and flagged low-confidence.
        if pool.n_sales < settings.min_exact_comps:
            card.derivation += (
                f"; only {pool.n_sales} individual sold comp(s), "
                f"fewer than {settings.min_exact_comps}: low confidence"
            )
            notes.append(
                f"only {pool.n_sales} individual sold comp(s) "
                f"(< {settings.min_exact_comps}); price low-confidence"
            )
        elif pool.undated:
            notes.append("sold comps have no sale dates; price low-confidence")
    elif active_est is not None:
        card.estimated_price = active_est
        card.price_basis = "active"
        card.price_source = "ebay_active"
        card.derivation = (
            f"based on {active_n} CURRENT ASKING price(s) (median ${active_est}); "
            "no sold-price data available"
        )
        if active_n < settings.min_exact_comps:
            notes.append(
                f"only {active_n} comp(s) (< {settings.min_exact_comps}); price low-confidence"
            )

    card.raw_value_estimate = card.estimated_price

    # --- Web fallback only if eBay yielded nothing ---
    if card.estimated_price is None:
        web = websearch.get_web_price_points(query)
        web_prices = [c.sold_price for c in web if c.sold_price]
        for c in web:
            db.add(_comp_row(card, c, "near", "web search result"))
        web_est, _ = _estimate(web_prices)
        if web_est is not None:
            card.estimated_price = web_est
            card.raw_value_estimate = web_est
            card.price_basis = "web"
            card.price_source = "web_search"
            card.derivation = f"web search median ${web_est}"

    # --- Graded upside (PSA 10 candidates): prefer sold, else active ---
    if card.psa10_candidate:
        g_sold: list[SoldComp] = []
        g_active: list[float] = []
        for s in partition(card, graded_comps):
            if s.match_type == "excluded":
                continue
            db.add(_comp_row(card, s.comp, "graded", s.match_reason))
            # Sources answer the graded query with raw sales mixed in (130point
            # ignores the grade entirely, SportsCardsPro returns its raw sales
            # table). Only an actually-graded sale may set the graded estimate.
            if s.match_type != "graded":
                continue
            if not s.comp.sold_price:
                continue
            if s.comp.kind == "sold":
                g_sold.append(s.comp)
            elif _within_recency(s.comp.sold_date, cutoff):
                g_active.append(s.comp.sold_price)
        g_pool = _sold_pool(g_sold, cutoff, settings.primary_sold_source)
        g_est, _ = _estimate(g_pool.prices or g_active)
        if g_est is not None:
            card.graded_value_estimate = g_est

    # Source failures (a rejected token, a blocked site) always lead: they are
    # the real reason a price is missing or thin. Only when every source
    # answered and still nothing priced the card is the identification the
    # likely culprit.
    problems, others = _split_reason("; ".join(notes))
    if card.estimated_price is None and not problems:
        others.insert(
            0,
            "no confident price match: verify the card's year/set/insert, "
            "then re-analyze",
        )
    # Always reflect THIS run: a clean re-price clears a stale reason.
    card.review_reason = _join_reason(problems, others)


def _route_status(card: Card, settings) -> Card:
    forced_review = card.psa10_candidate or card.anomaly_flag

    if card.estimated_price is None:
        return _flag_review(card, card.review_reason or "no matching eBay prices found")

    if forced_review:
        reasons = []
        if card.psa10_candidate:
            reasons.append("potential PSA 10: confirm grade before listing")
        if card.anomaly_flag:
            reasons.append("anomaly detected: confirm value before listing")
        # Source failures lead; the forced-review reasons must not bury them.
        problems, others = _split_reason(card.review_reason)
        card.status = STATUS_NEEDS_REVIEW
        card.review_reason = _join_reason(problems, reasons, others)
        return card

    if card.estimated_price < settings.min_store_value:
        card.status = STATUS_BELOW_THRESHOLD
        return card

    card.status = STATUS_PRICED
    return card


def _pick_reference_image(candidates: list[tuple[str, str, str]]) -> str | None:
    """Photo from an exact match only (eBay preferred); None otherwise."""
    # Accuracy-first: only use a photo from an EXACT match, so the comparison
    # image is always the SAME card — never an approximate/near match that could
    # mislead. If no exact match carried a photo, we show none.
    exact = [c for c in candidates if c[0] == "exact"]
    if not exact:
        return None
    exact.sort(key=lambda c: 0 if c[1].startswith("ebay") else 1)  # prefer eBay
    return exact[0][2]


def _scp_reference_image(card: Card) -> str | None:
    """The SportsCardsPro catalogue scan of the CONFIDENTLY-matched product.

    Accuracy-safe: the SCP lookup requires the card's number/parallel, so it can
    only resolve to the same card (never a different one). Returns None if SCP
    has no public image for it. Never raises.
    """
    from app.services import pricecharting

    try:
        return pricecharting.fetch_product_image(
            build_query(card),
            require_parallel=card.parallel,
            require_number=card.card_number,
            require_player=card.player,
        )
    except Exception:  # noqa: BLE001
        logger.exception("SportsCardsPro reference image lookup failed for card %s", card.id)
        return None


def _flag_review(card: Card, reason: str) -> Card:
    card.status = STATUS_NEEDS_REVIEW
    card.review_reason = reason
    return card


def _comp_row(card: Card, comp: SoldComp, match_type: str, reason: str) -> Comp:
    return Comp(
        card_id=card.id,
        title=comp.title,
        sold_price=comp.sold_price,
        sold_date=comp.sold_date,
        condition_grade=comp.condition_grade,
        listing_url=comp.listing_url,
        thumbnail_url=comp.thumbnail_url,
        match_type=match_type,
        match_reason=f"{reason} [{comp.kind}]",
        source=comp.source,
        marketplace=comp.marketplace,
    )
