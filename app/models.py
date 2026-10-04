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
# Soft-deleted: hidden everywhere, restorable for TRASH_RETENTION_DAYS, then
# purged (row and crop files) on the next startup. status_before_delete holds
# the status a restore puts back.
STATUS_DELETED = "deleted"
TRASH_RETENTION_DAYS = 7

# Listing row statuses. "published" is live on eBay; "ended" was withdrawn;
# "sold" was matched to an eBay order by the sold sync; "preview" and "failed"
# record attempts that never went live.
LISTING_PUBLISHED = "published"
LISTING_ENDED = "ended"
LISTING_SOLD = "sold"
LISTING_FAILED = "failed"
LISTING_PREVIEW = "preview"


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
    # The saved original's name inside data/inbox/processed (unique, path-safe).
    # Archive and tools/recrop_rotated.py read it; older rows have only
    # `filename`, which was the stored name for inbox ingests.
    stored_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # SHA-256 of the uploaded bytes, so the same photo is not processed twice.
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

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
    # Sport / category: baseball | football | basketball | hockey | soccer | other
    sport: Mapped[str | None] = mapped_column(String(32), nullable=True)
    set_brand: Mapped[str | None] = mapped_column(String(255), nullable=True)
    card_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parallel: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Insert / subset name ("League Leaders", "Record Breaker"); parallel is
    # kept for finish and numbering variants only (Gold, Refractor, /99).
    subset: Mapped[str | None] = mapped_column(String(255), nullable=True)
    team: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rookie: Mapped[bool] = mapped_column(Boolean, default=False)
    serial_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    condition: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    bbox_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    crop_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Which side this crop is. A library card is always a "front"; its matched
    # back image lives on back_crop_path. Un-matched backs stay as their own
    # "back" row (hidden from the collection) until a matching front arrives.
    side: Mapped[str] = mapped_column(String(8), default="front")
    back_crop_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    back_identification_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The front's own identity before a back overwrote it, so unmatching a wrong
    # back doesn't leave the card carrying another card's number/year/set.
    pre_pair_identity_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # EXIF DateTimeOriginal from the source photo — used for timestamp-based
    # front/back pairing (photos taken seconds apart are likely the same card).
    photo_taken_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Identification audit (raw read text, per-field confidence, verification result)
    identification_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Grading / anomaly
    grade_estimate: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gem_mint_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    psa10_candidate: Mapped[bool] = mapped_column(Boolean, default=False)
    grading_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Photo-quality read of the crop: "good", "glare", "blurry", "glare, blurry".
    photo_quality: Mapped[str | None] = mapped_column(String(32), nullable=True)
    anomaly_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    anomaly_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Pricing
    estimated_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_value_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    graded_value_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Real last-sold median (Marketplace Insights) and current-asking median (Browse).
    sold_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Highest RAW (ungraded) sold price among the matched comps — the top of the
    # recent sold range. Graded sales are excluded.
    sold_max_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
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
    # Soft delete (see STATUS_DELETED).
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status_before_delete: Mapped[str | None] = mapped_column(String(32), nullable=True)

    upload: Mapped["ImageUpload"] = relationship(back_populates="cards")
    comps: Mapped[list["Comp"]] = relationship(
        back_populates="card", cascade="all, delete-orphan"
    )
    listings: Mapped[list["Listing"]] = relationship(
        back_populates="card", cascade="all, delete-orphan"
    )

    @property
    def is_listed(self) -> bool:
        """True if this card has a real (published) eBay listing — distinct from
        its price status, so 'listed' and 'priced/below_threshold' can coexist."""
        return any(listing.status == "published" for listing in self.listings)

    @property
    def is_sold(self) -> bool:
        """True once the sold sync has matched an eBay order to this card."""
        return any(listing.status == LISTING_SOLD for listing in self.listings)

    @property
    def listing_state(self) -> str:
        """none | live | ended | sold (see ebay.orders.listing_state)."""
        from app.services.ebay.orders import listing_state

        return listing_state(self)

    @property
    def suggested_list_price(self) -> float | None:
        """The price a listing would use (basis-aware markup, floor, rounded up to 50 cents)."""
        from app.config import get_settings
        from app.services.ebay.listing_common import suggested_list_price

        return suggested_list_price(self, get_settings())

    @property
    def price_floor(self) -> float:
        """Lowest list price that still nets EBAY_MIN_NET after fees."""
        from app.config import get_settings
        from app.services.ebay.listing_common import listing_price_floor

        return listing_price_floor(get_settings())

    @property
    def has_back(self) -> bool:
        """True if a back-of-card image has been matched to this card."""
        return bool(self.back_crop_path)

    @property
    def ebay_listing_url(self) -> str | None:
        """Public eBay item URL for this card's published listing, if any."""
        for listing in self.listings:
            if listing.status == "published" and listing.listing_id:
                host = "www.ebay.com" if listing.ebay_mode == "live" else "sandbox.ebay.com"
                return f"https://{host}/itm/{listing.listing_id}"
        return None


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


class IdentificationCorrection(Base):
    """One identity field the user corrected by hand: what the model read, what
    the card said before the edit, and the final value. Exported as a golden
    set (tools/export_corrections.py) to measure identification accuracy.

    card_id is not a foreign key on purpose: the correction stays useful as a
    test case after the card itself is deleted."""

    __tablename__ = "identification_corrections"

    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[int] = mapped_column(Integer, index=True)
    field: Mapped[str] = mapped_column(String(32))
    model_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    crop_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    back_crop_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Listing(Base):
    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("cards.id"))
    ebay_mode: Mapped[str] = mapped_column(String(16))
    sku: Mapped[str | None] = mapped_column(String(64), nullable=True)
    offer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    listing_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    list_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16))  # published|ended|sold|failed|preview
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    # Set when the listing is withdrawn (POST /api/listings/{id}/end).
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Set by the sold sync from the matching eBay order. For a lot, every card's
    # row carries the whole lot's sale price.
    sold_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sold_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    card: Mapped["Card"] = relationship(back_populates="listings")


# Background job states. A job is one upload (one item per photo) or one
# "refresh prices" run (one item per card). A single worker thread runs items
# one at a time; see app/services/jobs.py.
JOB_UPLOAD = "upload"
JOB_REPRICE = "reprice"
ITEM_WAITING = "waiting"
ITEM_WORKING = "working"
ITEM_DONE = "done"
ITEM_FAILED = "failed"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    # Upload options: grid, batch tag, verify, force.
    params_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Hidden from /api/jobs/active once the user has seen its failures.
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False)

    items: Mapped[list["JobItem"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobItem.idx"
    )


class JobItem(Base):
    __tablename__ = "job_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    idx: Mapped[int] = mapped_column(Integer)
    # Display name: the photo's original filename, or the card's description.
    filename: Mapped[str] = mapped_column(String(512))
    state: Mapped[str] = mapped_column(String(16), default=ITEM_WAITING, index=True)
    step: Mapped[str | None] = mapped_column(String(255), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    card_ids_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    upload_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    card_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # A repeat upload skipped as already processed; retry with force=true.
    duplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    # Where a duplicate's bytes wait in case the user forces it.
    staged_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    job: Mapped["Job"] = relationship(back_populates="items")
