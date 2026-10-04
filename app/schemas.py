"""Pydantic schemas: Claude vision output + API request/response models."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


# --- Claude vision output ---------------------------------------------------
#
# The model's JSON is loose: a year comes back as 1989 instead of "1989", a
# confidence as null, a box as null. Pydantic 2 rejects all of those, and one
# rejected field used to sink every card in the photo. The validators below
# coerce what they safely can; vision.parse_detection then validates each card
# on its own, so a card that is still invalid is skipped alone.

# Confidence used when the model sends null: unknown is treated as low, so the
# card lands in review instead of being trusted.
UNKNOWN_CONFIDENCE = 0.3


def _as_text(v: Any) -> Any:
    """Numbers become text (1989 -> "1989", 1989.0 -> "1989"); others pass."""
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (int, float)):
        return str(v)
    return v


def _as_confidence(v: Any) -> Any:
    return UNKNOWN_CONFIDENCE if v is None else v


def _as_flag(v: Any) -> Any:
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "y", "1", "rc", "rookie")
    return v


class FieldRead(BaseModel):
    """A single identification field: the value Claude read + its confidence."""

    value: str | None = None
    confidence: float = 0.0

    _text = field_validator("value", mode="before")(_as_text)
    _conf = field_validator("confidence", mode="before")(_as_confidence)


class DetectedCard(BaseModel):
    """One card as returned by the vision model. Tolerant of missing fields."""

    player: str | None = None
    year: str | None = None
    # Sport / card category: baseball | football | basketball | hockey | soccer | other
    sport: str | None = None
    set_brand: str | None = None
    card_number: str | None = None
    # Finish or numbering variant only: "Refractor", "Gold /99", "Holo".
    parallel: str | None = None
    # Insert or subset name: "League Leaders", "Record Breaker", "All-Star".
    subset: str | None = None
    team: str | None = None
    # Rookie card (RC logo, "Rookie Card", "Star Rookie", first-year card).
    rookie: bool = False
    serial_number: str | None = None
    condition: str | None = None
    confidence: float = 0.0
    # "front" or "back" of the card (a back shows stats/number, no large photo).
    side: str | None = None
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

    _text = field_validator(
        "player", "year", "sport", "set_brand", "card_number", "parallel",
        "subset", "team", "serial_number", "condition", "side",
        "legibility_notes", "raw_text", "grade_estimate", "grading_notes",
        "anomaly_notes", mode="before",
    )(_as_text)
    _conf = field_validator("confidence", mode="before")(_as_confidence)
    _flags = field_validator(
        "psa10_candidate", "anomaly_flag", "rookie", mode="before"
    )(_as_flag)

    @field_validator("gem_mint_score", mode="before")
    @classmethod
    def _score(cls, v: Any) -> Any:
        return 0.0 if v is None else v

    @field_validator("bbox", mode="before")
    @classmethod
    def _bbox(cls, v: Any) -> Any:
        """A missing or malformed box becomes [] (no crop) instead of an error;
        the upload path drops a card it cannot crop."""
        if not isinstance(v, (list, tuple)) or len(v) != 4:
            return []
        try:
            return [float(x) for x in v]
        except (TypeError, ValueError):
            return []

    @field_validator("field_reads", mode="before")
    @classmethod
    def _reads(cls, v: Any) -> Any:
        """null -> {}; a bare value ("player": "Ken") -> {"value": "Ken"}; an
        entry that is neither is dropped rather than failing the card."""
        if not isinstance(v, dict):
            return {}
        out = {}
        for k, r in v.items():
            if isinstance(r, dict):
                out[k] = r
            elif r is None or isinstance(r, (str, int, float)):
                out[k] = {"value": r}
        return out


class VerificationCorrection(BaseModel):
    """One field the verifier believes is wrong.

    A correction is applied only when it carries a reason AND a confident value
    (see upload._verify_front); a bare value is only flagged."""

    value: str | None = None
    confidence: float | None = None
    reason: str | None = None

    _text = field_validator("value", mode="before")(_as_text)


class VerificationResult(BaseModel):
    # True = matches, False = something visible contradicts it, None = could not
    # confirm (e.g. the front does not print the year). Unknown is not
    # disagreement.
    agree: bool | None = True
    corrections: dict[str, VerificationCorrection] = Field(default_factory=dict)
    # Fields the verifier could not see well enough to confirm either way.
    unverifiable: list[str] = Field(default_factory=list)
    notes: str | None = None

    @field_validator("corrections", mode="before")
    @classmethod
    def _corrections(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            return {}
        out = {}
        for k, c in v.items():
            if isinstance(c, dict):
                out[k] = c
            elif c is not None:
                out[k] = {"value": c}
        return out

    @field_validator("unverifiable", mode="before")
    @classmethod
    def _unverifiable(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [v]
        return [str(x) for x in v] if isinstance(v, list) else []


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
    subset: str | None = None
    team: str | None = None
    rookie: bool | None = False
    serial_number: str | None = None
    condition: str | None = None
    confidence: float | None = None
    crop_path: str | None = None
    side: str = "front"
    has_back: bool = False
    grade_estimate: str | None = None
    gem_mint_score: float | None = None
    psa10_candidate: bool = False
    grading_notes: str | None = None
    photo_quality: str | None = None
    anomaly_flag: bool = False
    anomaly_notes: str | None = None
    estimated_price: float | None = None
    raw_value_estimate: float | None = None
    graded_value_estimate: float | None = None
    sold_estimate: float | None = None
    sold_max_estimate: float | None = None
    active_estimate: float | None = None
    price_basis: str | None = None
    price_source: str | None = None
    price_sources: str | None = None
    reference_image_url: str | None = None
    derivation: str | None = None
    excluded_count: int = 0
    status: str
    is_listed: bool = False
    ebay_listing_url: str | None = None
    review_reason: str | None = None
    # none | live | ended | sold (from the card's Listing rows).
    listing_state: str = "none"
    # The price a listing would use (same rule as the listing endpoints), and
    # the floor every list price is lifted to.
    suggested_list_price: float | None = None
    price_floor: float | None = None
    # Soft delete: set while the card is in the trash (restore within 7 days).
    deleted_at: datetime | None = None

    class Config:
        from_attributes = True


class CardDetailOut(CardOut):
    """Card plus full transparency payload."""

    identification_json: str | None = None
    bbox_json: str | None = None
    comps: list[CompOut] = Field(default_factory=list)


class UploadFileResult(BaseModel):
    upload_id: int | None = None
    filename: str
    card_count: int = 0
    error: str | None = None
    cards: list[CardOut] = Field(default_factory=list)
    # Existing fronts that gained one of this photo's backs.
    paired_into: list[int] = Field(default_factory=list)
    # Backs from this photo with no front yet (hidden until one arrives).
    backs_waiting: int = 0


class ManualCardRequest(BaseModel):
    player: str
    year: str | None = None
    sport: str | None = None
    set_brand: str | None = None
    card_number: str | None = None
    parallel: str | None = None
    subset: str | None = None
    team: str | None = None
    rookie: bool = False
    serial_number: str | None = None
    condition: str | None = None
    psa10_candidate: bool = False
    anomaly_flag: bool = False


class PromoteRequest(BaseModel):
    """Add one or more previewed cards to the repository."""

    card_ids: list[int]


class PriceFromUrlRequest(BaseModel):
    """Override pricing for a card from a pasted SportsCardsPro product URL."""

    url: str


class CardUpdateRequest(BaseModel):
    """Editable card metadata fields. Only non-None fields are applied."""

    player: str | None = None
    year: str | None = None
    sport: str | None = None
    set_brand: str | None = None
    card_number: str | None = None
    parallel: str | None = None
    subset: str | None = None
    team: str | None = None
    rookie: bool | None = None
    serial_number: str | None = None
    condition: str | None = None
    psa10_candidate: bool | None = None
    anomaly_flag: bool | None = None


class SellRequest(BaseModel):
    card_ids: list[int]
    prices: dict[str, float] | None = None


class SellResult(BaseModel):
    card_id: int
    status: str  # published | failed | skipped
    listing_id: str | None = None
    list_price: float | None = None
    message: str | None = None


class SellResponse(BaseModel):
    results: list[SellResult]


class SetSellResult(BaseModel):
    """Result of listing multiple cards as ONE combined lot listing."""

    status: str  # published | preview | failed
    listing_id: str | None = None
    sku: str | None = None
    list_price: float | None = None
    card_ids: list[int] = Field(default_factory=list)  # cards included in the lot
    skipped: list[str] = Field(default_factory=list)   # human-readable skip reasons
    message: str | None = None
