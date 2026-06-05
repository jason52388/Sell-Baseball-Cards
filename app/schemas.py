"""Pydantic schemas: Claude vision output + API request/response models."""
from __future__ import annotations

from pydantic import BaseModel, Field


# --- Claude vision output ---------------------------------------------------


class FieldRead(BaseModel):
    """A single identification field: the value Claude read + its confidence."""

    value: str | None = None
    confidence: float = 0.0


class DetectedCard(BaseModel):
    """One card as returned by the vision model. Tolerant of missing fields."""

    player: str | None = None
    year: str | None = None
    # Sport / card category: baseball | football | basketball | hockey | soccer | other
    sport: str | None = None
    set_brand: str | None = None
    card_number: str | None = None
    parallel: str | None = None
    serial_number: str | None = None
    condition: str | None = None
    confidence: float = 0.0
    bbox: list[float] = Field(default_factory=list)  # [x, y, w, h] normalized 0..1
    legibility_notes: str | None = None

    # Per-field reads (raw text + confidence) for the audit trail.
    field_reads: dict[str, FieldRead] = Field(default_factory=dict)
    raw_text: str | None = None

    # Grading / anomaly
    grade_estimate: str | None = None
    gem_mint_score: float = 0.0
    psa10_candidate: bool = False
    grading_notes: str | None = None
    anomaly_flag: bool = False
    anomaly_notes: str | None = None


class VerificationResult(BaseModel):
    agree: bool = True
    corrections: dict[str, str] = Field(default_factory=dict)
    notes: str | None = None


# --- API responses ----------------------------------------------------------


class CompOut(BaseModel):
    id: int
    title: str | None = None
    sold_price: float | None = None
    sold_date: str | None = None
    condition_grade: str | None = None
    listing_url: str | None = None
    thumbnail_url: str | None = None
    match_type: str
    match_reason: str | None = None
    source: str
    marketplace: str | None = None

    class Config:
        from_attributes = True


class CardOut(BaseModel):
    id: int
    upload_id: int
    batch_tag: str | None = None
    player: str | None = None
    year: str | None = None
    sport: str | None = None
    set_brand: str | None = None
    card_number: str | None = None
    parallel: str | None = None
    serial_number: str | None = None
    condition: str | None = None
    confidence: float | None = None
    crop_path: str | None = None
    grade_estimate: str | None = None
    gem_mint_score: float | None = None
    psa10_candidate: bool = False
    grading_notes: str | None = None
    anomaly_flag: bool = False
    anomaly_notes: str | None = None
    estimated_price: float | None = None
    raw_value_estimate: float | None = None
    graded_value_estimate: float | None = None
    sold_estimate: float | None = None
    active_estimate: float | None = None
    price_basis: str | None = None
    price_source: str | None = None
    price_sources: str | None = None
    reference_image_url: str | None = None
    derivation: str | None = None
    excluded_count: int = 0
    status: str
    review_reason: str | None = None

    class Config:
        from_attributes = True


class CardDetailOut(CardOut):
    """Card plus full transparency payload."""

    identification_json: str | None = None
    bbox_json: str | None = None
    comps: list[CompOut] = Field(default_factory=list)


class UploadCardSummary(BaseModel):
    card: CardOut


class UploadFileResult(BaseModel):
    upload_id: int | None = None
    filename: str
    card_count: int = 0
    error: str | None = None
    cards: list[CardOut] = Field(default_factory=list)


class UploadResponse(BaseModel):
    results: list[UploadFileResult]


class ManualCardRequest(BaseModel):
    player: str
    year: str | None = None
    sport: str | None = None
    set_brand: str | None = None
    card_number: str | None = None
    parallel: str | None = None
    serial_number: str | None = None
    condition: str | None = None
    psa10_candidate: bool = False
    anomaly_flag: bool = False


class PromoteRequest(BaseModel):
    """Add one or more previewed cards to the repository."""

    card_ids: list[int]


class SellRequest(BaseModel):
    card_ids: list[int]


class SellResult(BaseModel):
    card_id: int
    status: str  # published | failed | skipped
    listing_id: str | None = None
    list_price: float | None = None
    message: str | None = None


class SellResponse(BaseModel):
    results: list[SellResult]
