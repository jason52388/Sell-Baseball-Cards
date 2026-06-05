"""ORM models for uploads, cards, comps, and listings."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Card workflow statuses.
# A detected card starts as "preview": persisted (so crops/comps/reference photos
# work) but NOT yet in the user's library. The user explicitly promotes it via the
# add-to-repository action, which routes it to one of the statuses below.
STATUS_PREVIEW = "preview"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_PRICED = "priced"
STATUS_BELOW_THRESHOLD = "below_threshold"
STATUS_SELECTED = "selected"
STATUS_LISTED = "listed"
STATUS_LIST_FAILED = "list_failed"


class ImageUpload(Base):
    __tablename__ = "image_uploads"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(512))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    card_count: Mapped[int] = mapped_column(Integer, default=0)
    # Short user-supplied label for the batch (e.g. "1989 commons box").
    batch_tag: Mapped[str | None] = mapped_column(String(128), nullable=True)
    raw_vision_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    cards: Mapped[list["Card"]] = relationship(
        back_populates="upload", cascade="all, delete-orphan"
    )


class Card(Base):
    __tablename__ = "cards"

    id: Mapped[int] = mapped_column(primary_key=True)
    upload_id: Mapped[int] = mapped_column(ForeignKey("image_uploads.id"))
    # Short user-supplied label carried from the upload batch into the library.
    batch_tag: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Identification
    player: Mapped[str | None] = mapped_column(String(255), nullable=True)
    year: Mapped[str | None] = mapped_column(String(32), nullable=True)
    set_brand: Mapped[str | None] = mapped_column(String(255), nullable=True)
    card_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parallel: Mapped[str | None] = mapped_column(String(255), nullable=True)
    serial_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    condition: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    bbox_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    crop_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Identification audit (raw read text, per-field confidence, verification result)
    identification_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Grading / anomaly
    grade_estimate: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gem_mint_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    psa10_candidate: Mapped[bool] = mapped_column(Boolean, default=False)
    grading_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    anomaly_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    anomaly_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Pricing
    estimated_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_value_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    graded_value_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Real last-sold median (Marketplace Insights) and current-asking median (Browse).
    sold_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    active_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Which feeds estimated_price: "sold" | "active" | None.
    price_basis: Mapped[str | None] = mapped_column(String(16), nullable=True)
    price_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    derivation: Mapped[str | None] = mapped_column(Text, nullable=True)
    excluded_count: Mapped[int] = mapped_column(Integer, default=0)
    # Comma-separated list of marketplaces that had matching sold comps.
    price_sources: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # A reference photo of this card pulled from a matched marketplace listing.
    reference_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Workflow
    status: Mapped[str] = mapped_column(String(32), default=STATUS_NEEDS_REVIEW)
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    upload: Mapped["ImageUpload"] = relationship(back_populates="cards")
    comps: Mapped[list["Comp"]] = relationship(
        back_populates="card", cascade="all, delete-orphan"
    )
    listings: Mapped[list["Listing"]] = relationship(
        back_populates="card", cascade="all, delete-orphan"
    )


class Comp(Base):
    """One matching sold sale used to derive (or contextualize) a price."""

    __tablename__ = "comps"

    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("cards.id"))
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    sold_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    sold_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    condition_grade: Mapped[str | None] = mapped_column(String(64), nullable=True)
    listing_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumbnail_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    match_type: Mapped[str] = mapped_column(String(16), default="exact")  # exact|near|graded
    match_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Provider the data came through, e.g. "130point (sold)", "sportscardspro".
    source: Mapped[str] = mapped_column(String(48), default="ebay")
    # Original venue the sale happened on, e.g. "eBay", "PWCC", "Goldin".
    marketplace: Mapped[str | None] = mapped_column(String(32), nullable=True)

    card: Mapped["Card"] = relationship(back_populates="comps")


class PriceCache(Base):
    """Cached pooled comps for a card identity, to avoid re-hitting price APIs.

    Keyed by a normalized (marketplace|graded|query) string. Entries older than
    settings.price_cache_ttl_days are ignored and refreshed on next lookup. A
    refresh MERGES into the stored set (see comp_cache.merge_comps): dated sold
    sales accumulate as history; active/undated comps are replaced.
    """

    __tablename__ = "price_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    query_key: Mapped[str] = mapped_column(String(600), index=True, unique=True)
    # JSON-serialized list of SoldComp dicts (all sources pooled).
    payload_json: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Listing(Base):
    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("cards.id"))
    ebay_mode: Mapped[str] = mapped_column(String(16))
    sku: Mapped[str | None] = mapped_column(String(64), nullable=True)
    offer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    listing_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    list_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16))  # published|failed
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    card: Mapped["Card"] = relationship(back_populates="listings")
