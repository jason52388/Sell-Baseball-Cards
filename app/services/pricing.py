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

import statistics
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    STATUS_BELOW_THRESHOLD,
    STATUS_LIST_FAILED,
    STATUS_LISTED,
    STATUS_NEEDS_REVIEW,
    STATUS_PREVIEW,
    STATUS_PRICED,
    Card,
    Comp,
)
from app.services import comp_sources, ref_image, websearch
from app.services.ebay.base import SoldComp
from app.services.matching import partition

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


def _within_recency(sold_date: str | None, cutoff: date) -> bool:
    """Keep a comp if its date is recent OR unknown (don't drop undated comps)."""
    if not sold_date:
        return True
    try:
        d = datetime.fromisoformat(sold_date).date()
    except ValueError:
        return True
    return d >= cutoff


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


def _prefer_primary(
    entries: list[tuple[str, float]], primary: str
) -> tuple[list[float], str]:
    """Return (prices, label). If `primary` source has comps, use only those;
    otherwise use all. `entries` are (source, price)."""
    if not entries:
        return [], ""
    key = (primary or "").strip().lower()
    if key:
        primary_prices = [p for src, p in entries if src.lower().startswith(key)]
        if primary_prices:
            return primary_prices, primary
    sources = sorted({src for src, _ in entries if src})
    return [p for _, p in entries], ", ".join(sources) or "mixed"


def _gate(card: Card, settings) -> str | None:
    """Safeguard checks run before a card may be priced/promoted. Returns a
    review reason if the card should be flagged, else None."""
    if (card.confidence or 0.0) < settings.confidence_threshold:
        return "low identification confidence"
    if not _has_core_identity(card):
        return "incomplete identification"
    return None


def price_card(
    card: Card, db: Session, comp_fetcher: CompFetcher | None = None, *, refresh: bool = False
) -> Card:
    settings = get_settings()

    # --- Safeguards before pricing ---
    reason = _gate(card, settings)
    if reason:
        return _flag_review(card, reason)

    _compute_pricing(card, db, comp_fetcher, refresh=refresh)
    return _route_status(card, settings)


def preview_card(
    card: Card, db: Session, comp_fetcher: CompFetcher | None = None, *, refresh: bool = False
) -> Card:
    """Price a freshly detected card for *review* without promoting it.

    Runs the full comp gathering (so the marketplace reference photo and a
    tentative estimate appear even for low-confidence cards — the very ones the
    user needs to verify), but leaves the card in STATUS_PREVIEW. It enters the
    library only when the user explicitly promotes it via finalize_card.
    """
    if _has_core_identity(card):
        _compute_pricing(card, db, comp_fetcher, refresh=refresh)
        if card.estimated_price is None and not card.review_reason:
            # Identified, but no marketplace match was found for the query.
            card.review_reason = "no marketplace match for this identification"
    else:
        # Can't query without a player + (year or set) — tell the user why.
        card.review_reason = "incomplete identification — can't price; edit it manually"
    card.status = STATUS_PREVIEW
    return card


def reprice_after_pairing(card: Card, db: Session) -> Card:
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
    if card.status == STATUS_PREVIEW:
        return preview_card(card, db)
    if _has_core_identity(card):
        _compute_pricing(card, db)
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
    preview time — no comp re-fetch."""
    reason = _gate(card, settings)
    if reason:
        return _flag_review(card, reason)
    return _route_status(card, settings)


def _compute_pricing(
    card: Card, db: Session, comp_fetcher: CompFetcher | None = None, *, refresh: bool = False
) -> None:
    """Fetch comps, compute estimates, set the reference image, and write Comp
    rows. Mutates the card in place; does NOT gate or route status.
    `refresh` forces a live re-fetch instead of using cached comps."""
    settings = get_settings()
    notes: list[str] = []

    # Comp rows describe the CURRENT estimate, so a re-price replaces them.
    # Leaving this to callers meant several re-price paths piled up duplicates.
    for stale in list(card.comps):
        db.delete(stale)
    db.flush()

    query = build_query(card)
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=settings.comp_recency_days)

    # Comp fetch: injected fetcher (tests) returns a list; default returns notes too.
    if comp_fetcher is not None:
        raw_comps = comp_fetcher(query)

        def fetch_graded() -> list[SoldComp]:
            return comp_fetcher(query, graded=True)
    else:
        raw_comps, notes = comp_sources.gather_comps(
            query, refresh=refresh,
            require_parallel=card.parallel, require_number=card.card_number,
        )

        def fetch_graded() -> list[SoldComp]:
            return comp_sources.gather_comps(
                query, graded=True, refresh=refresh,
                require_parallel=card.parallel, require_number=card.card_number,
            )[0]

    scored = partition(card, raw_comps)
    card.excluded_count = sum(1 for s in scored if s.match_type == "excluded")

    sold_entries: list[tuple[str, float]] = []  # (source, price)
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
        if (
            s.match_type == "exact"
            and s.comp.sold_price
            and _within_recency(s.comp.sold_date, cutoff)
        ):
            if s.comp.kind == "sold":
                sold_entries.append((s.comp.source or "", s.comp.sold_price))
            else:
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

    # Prefer the configured primary sold source (e.g. PriceCharting); fall back
    # to all sold comps if the primary returned nothing.
    sold_prices, sold_from = _prefer_primary(sold_entries, settings.primary_sold_source)
    sold_est, sold_n = _estimate(sold_prices)
    active_est, active_n = _estimate(active_prices)
    card.sold_estimate = sold_est
    # Top of the raw (ungraded) sold range — sold_prices already excludes graded.
    card.sold_max_estimate = round(max(sold_prices), 2) if sold_prices else None
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
        chosen_n = sold_n
        card.derivation = (
            f"based on {sold_n} recent SOLD price(s) from {sold_from} (median ${sold_est})"
            + (f"; current asking median ${active_est}" if active_est else "")
        )
    elif active_est is not None:
        card.estimated_price = active_est
        card.price_basis = "active"
        card.price_source = "ebay_active"
        chosen_n = active_n
        card.derivation = (
            f"based on {active_n} CURRENT ASKING price(s) (median ${active_est}); "
            "no sold-price data available"
        )
    else:
        chosen_n = 0

    card.raw_value_estimate = card.estimated_price

    if card.estimated_price is not None and chosen_n < settings.min_exact_comps:
        notes.append(
            f"only {chosen_n} comp(s) (< {settings.min_exact_comps}); price low-confidence"
        )

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
        g_sold: list[tuple[str, float]] = []
        g_active: list[float] = []
        for s in partition(card, fetch_graded()):
            if s.match_type == "excluded":
                continue
            db.add(_comp_row(card, s.comp, "graded", s.match_reason))
            # Sources answer the graded query with raw sales mixed in (130point
            # ignores the grade entirely, SportsCardsPro returns its raw sales
            # table). Only an actually-graded sale may set the graded estimate.
            if s.match_type != "graded":
                continue
            if s.comp.sold_price and _within_recency(s.comp.sold_date, cutoff):
                if s.comp.kind == "sold":
                    g_sold.append((s.comp.source or "", s.comp.sold_price))
                else:
                    g_active.append(s.comp.sold_price)
        g_sold_prices, _ = _prefer_primary(g_sold, settings.primary_sold_source)
        g_est, _ = _estimate(g_sold_prices or g_active)
        if g_est is not None:
            card.graded_value_estimate = g_est

    # When nothing could price the card, lead with a clear, specific reason —
    # not the generic "Insights off" note (which appears on every card and
    # misleads here). The other notes follow as secondary context.
    if card.estimated_price is None:
        notes.insert(
            0,
            "no confident price match — verify the card's year/set/insert, "
            "then re-analyze",
        )

    if notes:
        card.review_reason = "; ".join(notes)


def _route_status(card: Card, settings) -> Card:
    forced_review = card.psa10_candidate or card.anomaly_flag

    if card.estimated_price is None:
        return _flag_review(card, card.review_reason or "no matching eBay prices found")

    if forced_review:
        reasons = []
        if card.psa10_candidate:
            reasons.append("potential PSA 10 — confirm grade before listing")
        if card.anomaly_flag:
            reasons.append("anomaly detected — confirm value before listing")
        if card.review_reason:
            reasons.append(card.review_reason)
        card.status = STATUS_NEEDS_REVIEW
        card.review_reason = "; ".join(reasons)
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
        )
    except Exception:  # noqa: BLE001
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
