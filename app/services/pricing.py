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
    STATUS_NEEDS_REVIEW,
    STATUS_PREVIEW,
    STATUS_PRICED,
    Card,
    Comp,
)
from app.services import comp_sources, websearch
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


def price_card(card: Card, db: Session, comp_fetcher: CompFetcher | None = None) -> Card:
    settings = get_settings()

    # --- Safeguards before pricing ---
    reason = _gate(card, settings)
    if reason:
        return _flag_review(card, reason)

    _compute_pricing(card, db, comp_fetcher)
    return _route_status(card, settings)


def preview_card(card: Card, db: Session, comp_fetcher: CompFetcher | None = None) -> Card:
    """Price a freshly detected card for *review* without promoting it.

    Runs the full comp gathering (so the marketplace reference photo and a
    tentative estimate appear even for low-confidence cards — the very ones the
    user needs to verify), but leaves the card in STATUS_PREVIEW. It enters the
    library only when the user explicitly promotes it via finalize_card.
    """
    if _has_core_identity(card):
        _compute_pricing(card, db, comp_fetcher)
        if card.estimated_price is None and not card.review_reason:
            # Identified, but no marketplace match was found for the query.
            card.review_reason = "no marketplace match for this identification"
    else:
        # Can't query without a player + (year or set) — tell the user why.
        card.review_reason = "incomplete identification — can't price; edit it manually"
    card.status = STATUS_PREVIEW
    return card


def finalize_card(card: Card, settings) -> Card:
    """Promote a previewed card into the library, applying the same safeguards
    and status routing as a normal price. Reuses the estimate already computed at
    preview time — no comp re-fetch."""
    reason = _gate(card, settings)
    if reason:
        return _flag_review(card, reason)
    return _route_status(card, settings)


def _compute_pricing(card: Card, db: Session, comp_fetcher: CompFetcher | None = None) -> None:
    """Fetch comps, compute estimates, set the reference image, and write Comp
    rows. Mutates the card in place; does NOT gate or route status."""
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
        raw_comps, notes = comp_sources.gather_comps(query)

        def fetch_graded() -> list[SoldComp]:
            return comp_sources.gather_comps(query, graded=True)[0]

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

    if matched_sources:
        card.price_sources = ", ".join(matched_sources)
    card.reference_image_url = _pick_reference_image(ref_candidates)

    # Prefer the configured primary sold source (e.g. PriceCharting); fall back
    # to all sold comps if the primary returned nothing.
    sold_prices, sold_from = _prefer_primary(sold_entries, settings.primary_sold_source)
    sold_est, sold_n = _estimate(sold_prices)
    active_est, active_n = _estimate(active_prices)
    card.sold_estimate = sold_est
    card.active_estimate = active_est

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
            if s.comp.sold_price and _within_recency(s.comp.sold_date, cutoff):
                if s.comp.kind == "sold":
                    g_sold.append((s.comp.source or "", s.comp.sold_price))
                else:
                    g_active.append(s.comp.sold_price)
        g_sold_prices, _ = _prefer_primary(g_sold, settings.primary_sold_source)
        g_est, _ = _estimate(g_sold_prices or g_active)
        if g_est is not None:
            card.graded_value_estimate = g_est

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
    """Prefer an exact eBay match's photo, then any exact, then anything."""
    if not candidates:
        return None

    def rank(c: tuple[str, str, str]) -> tuple[int, int]:
        match_type, source, _ = c
        return (0 if match_type == "exact" else 1, 0 if source.startswith("ebay") else 1)

    return min(candidates, key=rank)[2]


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
    )
