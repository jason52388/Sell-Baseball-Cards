"""Shared helpers for building eBay listing payloads from a Card.

eBay constraints encoded here (verified against eBay's 2026 listing docs):
  - TITLE      : 80 characters max
  - IMAGES     : 24 per listing max; URLs must be HTTPS for the Inventory API
  - DESCRIPTION: 500,000 characters max (HTML allowed)
  - ASPECT     : 65 chars per value, 30 values per aspect name
  - LOT CATEGORY: 261329 (Sports Trading Card Lots) with a "Number of Cards" aspect
  - CONDITION  : trading-card categories accept only two conditions:
                   Graded   = LIKE_NEW (conditionId 2750) + descriptors 27501/27502
                   Ungraded = USED_VERY_GOOD (conditionId 4000) + descriptor 40001
                 (eBay "Condition descriptor IDs for trading cards")

The preview client and the real Sell API client both build their payloads with
`build_single_payload` / `build_lot_payload`, so a preview is exactly what a
real publish sends (only the image URLs differ: the real client swaps in eBay
Picture Services URLs, see media.py).
"""
from __future__ import annotations

import hashlib
import html
import math
import re
from pathlib import Path

MAX_TITLE = 80
MAX_IMAGES = 24
MAX_DESCRIPTION = 500_000
MAX_ASPECT_VALUE = 65
MAX_ASPECT_VALUES = 30

# --- Condition ----------------------------------------------------------------

CONDITION_UNGRADED = "USED_VERY_GOOD"  # conditionId 4000
CONDITION_GRADED = "LIKE_NEW"          # conditionId 2750

# Ungraded: Card Condition descriptor (40001). eBay's fixed value enums; there is
# no "Good", so it maps to Very good.
CARD_CONDITION_DESCRIPTOR = "40001"
_CARD_CONDITION_VALUES = {
    "mint": "400010",         # Near mint or better
    "near-mint": "400010",
    "near mint": "400010",
    "excellent": "400011",    # Excellent
    "very good": "400012",    # Very good
    "good": "400012",
    "poor": "400013",         # Poor
}
_DEFAULT_CARD_CONDITION = "400010"
# Worst first, used to pick the condition a lot is described by.
_CONDITION_RANK = {"400013": 0, "400012": 1, "400011": 2, "400010": 3}

# Graded: Professional Grader (27501) + Grade (27502) value ids, from eBay's
# "Condition descriptor IDs for trading cards" table.
GRADER_DESCRIPTOR = "27501"
GRADE_DESCRIPTOR = "27502"
_GRADERS = {
    "PSA": ("275010", "Professional Sports Authenticator (PSA)"),
    "BCCG": ("275011", "Beckett Collectors Club Grading (BCCG)"),
    "BVG": ("275012", "Beckett Vintage Grading (BVG)"),
    "BGS": ("275013", "Beckett Grading Services (BGS)"),
    "CSG": ("275014", "Certified Sports Guaranty (CSG)"),
    "CGC": ("275015", "Certified Guaranty Company (CGC)"),
    "SGC": ("275016", "Sportscard Guaranty Corporation (SGC)"),
    "KSA": ("275017", "K Sportscard Authentication (KSA)"),
    "GMA": ("275018", "Gem Mint Authentication (GMA)"),
    "HGA": ("275019", "Hybrid Grading Approach (HGA)"),
    "ISA": ("2750110", "International Sports Authentication (ISA)"),
    "PCA": ("2750111", "Professional Card Authenticator (PCA)"),
    "TAG": ("2750115", "Technical Authentication & Grading (TAG)"),
}
# 275020 = 10, 275021 = 9.5, ... 2750218 = 1 (half-point steps, descending).
_GRADES = {f"{10 - 0.5 * k:g}": f"27502{k}" for k in range(19)}
_GRADES["Authentic"] = "2750219"
_SLAB_RE = re.compile(
    r"\b(" + "|".join(_GRADERS) + r")\s*(?:GEM\s*MT|GEM\s*MINT|MINT|NM-MT|NM)?\s*"
    r"(10|[1-9](?:\.5)?|Authentic)\b",
    re.IGNORECASE,
)


def slab_grade(card) -> tuple[str, str] | None:
    """(grader, grade) when the card's CONDITION says it is in a slab, e.g.
    "PSA 9" or "BGS 9.5". A grade estimate (grade_estimate / psa10_candidate) is
    a guess about a raw card, never a slab, so it is not consulted."""
    m = _SLAB_RE.search(str(getattr(card, "condition", None) or ""))
    if not m:
        return None
    grader = m.group(1).upper()
    grade = m.group(2)
    grade = "Authentic" if grade.lower() == "authentic" else grade
    if grade not in _GRADES:
        return None
    return grader, grade


def _raw_condition_value(card) -> str:
    cond = (getattr(card, "condition", None) or "").strip().lower()
    return _CARD_CONDITION_VALUES.get(cond, _DEFAULT_CARD_CONDITION)


def map_condition(card, default: str = CONDITION_UNGRADED) -> str:
    """eBay condition enum. Only two are valid in trading-card categories, so
    the card's free-text condition only feeds the descriptor, never this.
    `default` is accepted for backward compatibility and ignored."""
    return CONDITION_GRADED if slab_grade(card) else CONDITION_UNGRADED


def build_condition_descriptors(card) -> list[dict]:
    """Descriptors eBay requires next to the condition enum (publishOffer fails
    without them): grader + grade for a slab, Card Condition for a raw card."""
    graded = slab_grade(card)
    if graded:
        grader, grade = graded
        return [
            {"name": GRADER_DESCRIPTOR, "values": [_GRADERS[grader][0]]},
            {"name": GRADE_DESCRIPTOR, "values": [_GRADES[grade]]},
        ]
    return [{"name": CARD_CONDITION_DESCRIPTOR, "values": [_raw_condition_value(card)]}]


def worst_condition_card(cards):
    """The card in a lot with the worst raw condition: a lot is described by its
    weakest card so no buyer is promised better than they get."""
    return min(cards, key=lambda c: _CONDITION_RANK.get(_raw_condition_value(c), 3))


# --- Identity helpers -----------------------------------------------------------


def _val(card, name: str):
    """A field's cleaned value, or None. getattr so optional attributes (subset,
    team, rookie) that may not exist on every Card version are tolerated."""
    v = getattr(card, name, None)
    if v is None or isinstance(v, bool):
        return v
    s = str(v).strip()
    return None if s.lower() in ("", "none", "null") else s


# Checked in order, so Bowman resolves to Topps and the Panini-era brands
# (Donruss, Prizm, Select, Optic) resolve to Panini.
_MANUFACTURERS = [
    (("topps", "bowman", "stadium club"), "Topps"),
    (("upper deck",), "Upper Deck"),
    (("fleer", "skybox"), "Fleer"),
    (("donruss", "panini", "prizm", "optic", "playoff"), "Panini"),
    (("score",), "Score"),
    (("pinnacle",), "Pinnacle"),
    (("pacific",), "Pacific"),
    (("leaf",), "Leaf"),
]


def manufacturer(set_brand) -> str | None:
    s = (str(set_brand) if set_brand else "").lower()
    if not s:
        return None
    for keys, maker in _MANUFACTURERS:
        if any(re.search(rf"\b{re.escape(k)}\b", s) for k in keys):
            return maker
    return None


_LEAGUES = {
    "baseball": "Major League (MLB)",
    "basketball": "NBA",
    "football": "NFL",
    "hockey": "NHL",
}


def _year_int(card) -> int | None:
    m = re.search(r"\b(18|19|20)\d{2}\b", str(getattr(card, "year", None) or ""))
    return int(m.group(0)) if m else None


def _print_run(card) -> str | None:
    """Denominator of a serial number: "23/99" -> "99", "/50" -> "50"."""
    m = re.search(r"/\s*(\d+)", _val(card, "serial_number") or "")
    return m.group(1) if m else None


# --- Title ----------------------------------------------------------------------


def build_title(card) -> str:
    """Title by priority, trimmed by whole words to eBay's 80 characters.

    Order: year, set, player, subset/insert, parallel, #number, serial "/99",
    RC, team, sport word. A segment that does not fit is skipped (the core
    set/player segments keep as many leading words as fit); a word is never cut.
    """
    run = _print_run(card)
    sport = (_val(card, "sport") or "baseball").title()
    number = _val(card, "card_number")
    segments = [
        (_val(card, "year"), False),
        (_val(card, "set_brand"), True),
        (_val(card, "player"), True),
        (_val(card, "subset"), False),
        (_val(card, "parallel"), False),
        (f"#{number}" if number else None, False),
        (f"/{run}" if run else None, False),
        ("RC" if getattr(card, "rookie", None) is True else None, False),
        (_val(card, "team"), False),
        (f"{sport} Card", False),
    ]
    words: list[str] = []

    def fits(extra: list[str]) -> bool:
        return len(" ".join(words + extra)) <= MAX_TITLE

    for seg, core in segments:
        if not seg:
            continue
        seg_words = seg.split()
        if fits(seg_words):
            words += seg_words
        elif core:
            for w in seg_words:
                if not fits([w]):
                    break
                words.append(w)
    title = " ".join(words).strip()
    return title or f"{sport} Card {getattr(card, 'id', '')}".strip()


# --- Item specifics -------------------------------------------------------------


def build_aspects(card) -> dict[str, list[str]]:
    """Item specifics for a single card. Only aspects with real values are sent
    (eBay rejects empty ones); every value is capped at 65 characters.
    "Sport" is REQUIRED by eBay's Baseball Cards category."""
    sport = _val(card, "sport") or "baseball"
    year = _year_int(card)
    graded = slab_grade(card)
    run = _print_run(card)
    features = []
    if getattr(card, "rookie", None) is True:
        features.append("Rookie")
    if run:
        features.append("Serial Numbered")
    if _val(card, "parallel"):
        features.append("Parallel/Variety")
    candidates: dict[str, object] = {
        "Sport": sport.title(),
        "Type": "Sports Trading Card",
        "Player/Athlete": _val(card, "player"),
        "Card Name": _val(card, "player"),
        "Manufacturer": manufacturer(_val(card, "set_brand")),
        "Set": _val(card, "set_brand"),
        "Season": _val(card, "year"),
        "Year Manufactured": str(year) if year else None,
        "Card Number": _val(card, "card_number"),
        "Parallel/Variety": _val(card, "parallel"),
        "Insert Set": _val(card, "subset"),
        "Team": _val(card, "team"),
        "League": _LEAGUES.get(sport.lower()),
        "Print Run": run,
        "Features": features or None,
        "Graded": "Yes" if graded else "No",
        "Professional Grader": _GRADERS[graded[0]][1] if graded else None,
        "Grade": graded[1] if graded else None,
        "Autographed": "Yes" if getattr(card, "autographed", None) is True else "No",
        "Vintage": ("Yes" if year < 1980 else "No") if year else None,
        "Original/Licensed Reprint": "Original",
    }
    out: dict[str, list[str]] = {}
    for name, value in candidates.items():
        values = value if isinstance(value, list) else [value]
        values = [str(v)[:MAX_ASPECT_VALUE] for v in values if v not in (None, "")]
        if values:
            out[name] = values[:MAX_ASPECT_VALUES]
    return out


# --- Images -----------------------------------------------------------------------


def listing_image_paths(card) -> list[str]:
    """Local photo files for a card's listing: the front crop, then the back."""
    paths = (getattr(card, "crop_path", None), getattr(card, "back_crop_path", None))
    return [p for p in paths if p]


def public_crop_url(path: str | None, base: str | None) -> str | None:
    """Public URL of a local crop through the app's /crops mount."""
    if not path or not base:
        return None
    return f"{base.rstrip('/')}/crops/{Path(path).name}"


def card_image_url(card, base: str) -> str | None:
    """Public URL of the front crop, or None if we have nothing to show."""
    return public_crop_url(getattr(card, "crop_path", None), base)


def reference_image_url(card, base: str) -> str | None:
    ref = getattr(card, "reference_image_url", None)
    if not ref:
        return None
    if ref.startswith("/refimg/") and base:
        return f"{base.rstrip('/')}{ref}"
    if ref.startswith("https://"):
        return ref
    return None


def card_image_urls(card, base: str, include_reference: bool = False) -> list[str]:
    """Public URLs of the card's own photos (front, then back). The reference
    photo belongs to another seller, so it is added only when asked for
    (EBAY_INCLUDE_REFERENCE_IMAGE)."""
    urls = [u for u in (public_crop_url(p, base) for p in listing_image_paths(card)) if u]
    if include_reference:
        ref = reference_image_url(card, base)
        if ref:
            urls.append(ref)
    return urls[:MAX_IMAGES]


# --- Description --------------------------------------------------------------------


def build_description(card) -> str:
    """A simple, honest HTML description for a single card: its identity, the
    condition, and a see-the-photos note. Capped at eBay's limit."""
    graded = slab_grade(card)
    rows = [
        ("Year", _val(card, "year")),
        ("Set", _val(card, "set_brand")),
        ("Player", _val(card, "player")),
        ("Card #", _val(card, "card_number")),
        ("Insert", _val(card, "subset")),
        ("Parallel", _val(card, "parallel")),
        ("Serial #", _val(card, "serial_number")),
        ("Team", _val(card, "team")),
        ("Sport", (_val(card, "sport") or "baseball").title()),
        ("Condition (graded)" if graded else "Condition (raw, ungraded)", _val(card, "condition")),
    ]
    items = "".join(
        f"<li><strong>{html.escape(label)}:</strong> {html.escape(str(val))}</li>"
        for label, val in rows
        if val
    )
    note = (
        "Professionally graded card in its slab. Please review the photos."
        if graded
        else "Raw (ungraded) card. Please review the photos for exact condition: "
        "what you see is what you get."
    )
    return (
        f"<h2>{html.escape(build_title(card))}</h2>"
        f"<ul>{items}</ul>"
        f"<p>{note} Ships securely in a penny sleeve and top-loader, packaged to "
        "arrive safely.</p>"
    )[:MAX_DESCRIPTION]


# --- Price --------------------------------------------------------------------------


def _money(x: float) -> str:
    return f"{x:.2f}"


def listing_price_floor(settings) -> float:
    """Lowest list price that still nets EBAY_MIN_NET after eBay's fee on the
    sale, the per-order fee, and shipping supplies. Rounded UP to the cent."""
    fixed = (
        settings.ebay_per_order_fee
        + settings.ebay_shipping_supplies_cost
        + settings.ebay_min_net
    )
    raw = fixed / max(1e-6, 1 - settings.ebay_fee_pct)
    return math.ceil(round(raw * 100, 6)) / 100


def round_up_half(price: float, floor: float = 0.0) -> float:
    """Round UP to the next 50 cents ($12.10 -> $12.50, $12.60 -> $13.00), never
    below `floor` (itself rounded up) and never below $0.50. Already on a half
    dollar stays put."""
    target = max(price, floor, 0.50)
    return math.ceil(round(target * 2, 6)) / 2


def base_list_price(card, settings) -> float | None:
    """List price before the floor and rounding, by price basis:
    sold comps   -> estimate x PRICE_MARKUP (sales are what the card is worth)
    asking comps -> median ask x EBAY_ASK_UNDERCUT (asks already sit above sold,
                    so a markup on top would price the card out of the market)
    """
    est = getattr(card, "estimated_price", None)
    if not est:
        return None
    if (getattr(card, "price_basis", None) or "").lower() == "active":
        ask = getattr(card, "active_estimate", None) or est
        return ask * settings.ebay_ask_undercut
    return est * settings.price_markup


def suggested_list_price(card, settings) -> float | None:
    """The list price for one card: base_list_price, never below the floor,
    rounded up to the next 50 cents. None when the card has no estimate. Used everywhere a list
    price is computed (listing endpoints, GET /api/listings/{id} for the UI)."""
    base = base_list_price(card, settings)
    if base is None:
        return None
    return round_up_half(base, listing_price_floor(settings))


def suggested_lot_price(cards, settings) -> float | None:
    """A lot sells once (one order fee, one mailer), so the floor applies once
    to the sum of the cards' base prices."""
    bases = [b for b in (base_list_price(c, settings) for c in cards) if b is not None]
    if not bases:
        return None
    return round_up_half(sum(bases), listing_price_floor(settings))


def best_offer_terms(list_price: float, settings) -> dict | None:
    """Best Offer: auto-accept at max(EBAY_BEST_OFFER_AUTO_ACCEPT_PCT x list,
    floor); auto-decline below the floor. None (Best Offer off) when the list
    price leaves no room, since eBay needs auto-accept below the price."""
    floor = listing_price_floor(settings)
    accept = round(max(list_price * settings.ebay_best_offer_auto_accept_pct, floor), 2)
    if accept >= list_price:
        return None
    terms = {
        "bestOfferEnabled": True,
        "autoAcceptPrice": {"value": _money(accept), "currency": "USD"},
    }
    if floor < accept:
        terms["autoDeclinePrice"] = {"value": _money(floor), "currency": "USD"}
    return terms


# --- Payloads (the ONE builder preview and real publish share) ----------------------


def card_sku(card) -> str:
    return f"CARD-{card.id}"


def ships_by_envelope(settings, list_price, card_count: int = 1) -> bool:
    """eBay Standard Envelope: a single card priced up to EBAY_ENVELOPE_MAX_PRICE
    ($20, eBay's limit), when an envelope policy is configured. Lots are too
    thick for a letter envelope, so they always ship as a parcel."""
    return (
        bool(settings.ebay_envelope_fulfillment_policy_id)
        and card_count == 1
        and list_price <= settings.ebay_envelope_max_price
    )


def build_offer_payload(settings, sku, list_price, *, category_id=None, description=None,
                        envelope=False) -> dict:
    policy = (settings.ebay_envelope_fulfillment_policy_id if envelope
              else settings.ebay_fulfillment_policy_id)
    payload = {
        "sku": sku,
        "marketplaceId": settings.ebay_marketplace_id,
        "format": "FIXED_PRICE",
        "availableQuantity": 1,
        "categoryId": category_id or settings.ebay_category_id,
        "listingPolicies": {
            "fulfillmentPolicyId": policy,
            "paymentPolicyId": settings.ebay_payment_policy_id,
            "returnPolicyId": settings.ebay_return_policy_id,
        },
        "merchantLocationKey": settings.ebay_merchant_location_key,
        "pricingSummary": {"price": {"value": _money(list_price), "currency": "USD"}},
    }
    terms = best_offer_terms(list_price, settings)
    if terms:
        payload["listingPolicies"]["bestOfferTerms"] = terms
    if description:
        payload["listingDescription"] = description
    return payload


def package_weight_and_size(settings, card_count: int = 1, *, envelope=False) -> dict:
    """Shipped package for a calculated-shipping policy: eBay refuses to publish
    without a weight, since it prices postage from weight and buyer zip. One
    card in a toploader and bubble mailer, plus a little for each extra card.
    A Standard Envelope card is a plain letter: up to 3 oz and 1/4 inch thick."""
    if envelope:
        return {
            "packageType": "LETTER",
            "weight": {"value": round(settings.ebay_envelope_weight_oz, 2), "unit": "OUNCE"},
            "dimensions": {"length": 6.5, "width": 3.63, "height": 0.25, "unit": "INCH"},
        }
    oz = settings.ebay_package_weight_oz + settings.ebay_lot_extra_card_weight_oz * max(0, card_count - 1)
    return {
        "packageType": "PACKAGE_THICK_ENVELOPE",
        "weight": {"value": round(oz, 2), "unit": "OUNCE"},
        "dimensions": {"length": 7.0, "width": 4.0, "height": 1.0, "unit": "INCH"},
    }


def _inventory_item(title, aspects, condition_card, image_urls, package) -> dict:
    product: dict = {"title": title, "aspects": aspects}
    if image_urls:
        product["imageUrls"] = list(image_urls)[:MAX_IMAGES]
    return {
        "product": product,
        "condition": map_condition(condition_card),
        "conditionDescriptors": build_condition_descriptors(condition_card),
        "packageWeightAndSize": package,
        "availability": {"shipToLocationAvailability": {"quantity": 1}},
    }


def build_single_payload(card, list_price, settings, image_urls) -> dict:
    """{"sku", "inventory_item", "offer"} for one card: the inventory item body
    (PUT /inventory_item/{sku}) and the offer body (POST /offer)."""
    sku = card_sku(card)
    envelope = ships_by_envelope(settings, list_price)
    return {
        "sku": sku,
        "inventory_item": _inventory_item(
            build_title(card), build_aspects(card), card, image_urls,
            package_weight_and_size(settings, envelope=envelope),
        ),
        "offer": build_offer_payload(
            settings, sku, list_price, description=build_description(card),
            envelope=envelope,
        ),
    }


def build_lot_payload(cards, list_price, settings, image_urls) -> dict:
    """Same shape as build_single_payload, for a lot of several cards."""
    sku = set_sku(cards)
    return {
        "sku": sku,
        "inventory_item": _inventory_item(
            build_set_title(cards), build_set_aspects(cards),
            worst_condition_card(cards), image_urls,
            package_weight_and_size(settings, len(cards)),
        ),
        "offer": build_offer_payload(
            settings, sku, list_price, category_id=settings.ebay_lot_category_id,
            description=build_set_description(cards, shown_images=len(image_urls)),
        ),
    }


# --- SET / LOT listings: combine N cards into a single eBay listing -----------


def _clean(value) -> str:
    return str(value).strip()


def _distinct(values) -> list[str]:
    """Order-preserving de-dupe of non-empty values."""
    seen: dict[str, None] = {}
    for v in values:
        c = _clean(v)
        if c and c.lower() != "none":
            seen.setdefault(c, None)
    return list(seen)


def build_set_title(cards) -> str:
    """A readable lot title, capped at eBay's 80-char limit.

    Leads with the card count + sport ("12-Card Baseball Card Lot"), then fills
    the remaining space with distinct sets and a few player names.
    """
    n = len(cards)
    sport = (_distinct(c.sport for c in cards) or ["Sports"])[0].title()
    title = f"{n}-Card {sport} Card Lot"
    sets = _distinct(f"{c.year or ''} {c.set_brand or ''}".strip() for c in cards)
    players = _distinct(c.player for c in cards)
    # Append optional segments only while they still fit within 80 chars.
    for seg in (", ".join(sets[:3]), "(" + ", ".join(players[:4]) + ")" if players else ""):
        if seg and len(title) + 3 + len(seg) <= MAX_TITLE:
            title = f"{title} - {seg}"
    return title[:MAX_TITLE]


def build_set_aspects(cards) -> dict[str, list[str]]:
    """Item specifics for a lot. 'Number of Cards' is expected for category 261329.
    Multi-value aspects (players/sets/seasons) are de-duped and capped to eBay's
    30-values / 65-chars-per-value limits."""
    def capped(values: list[str]) -> list[str]:
        return [v[:MAX_ASPECT_VALUE] for v in values[:MAX_ASPECT_VALUES]]

    aspects: dict[str, list[str]] = {"Number of Cards": [str(len(cards))]}
    sport = _distinct(c.sport for c in cards)
    if sport:
        aspects["Sport"] = capped([s.title() for s in sport])
    for name, vals in (
        ("Player/Athlete", _distinct(c.player for c in cards)),
        ("Set", _distinct(c.set_brand for c in cards)),
        ("Season", _distinct(c.year for c in cards)),
    ):
        if vals:
            aspects[name] = capped(vals)
    return aspects


def build_set_description(cards, *, shown_images: int | None = None) -> str:
    """HTML table describing every card in the lot (well under the 500K limit)."""
    n = len(cards)
    rows = []
    for i, c in enumerate(cards, 1):
        cells = [
            i,
            html.escape(_clean(c.year) or "—"),
            html.escape(_clean(c.set_brand) or "—"),
            html.escape(_clean(c.player) or "—"),
            html.escape(_clean(c.card_number) or "—"),
            html.escape(_clean(c.parallel) or "—"),
            html.escape(_clean(c.condition) or "—"),
        ]
        rows.append("<tr>" + "".join(f"<td>{v}</td>" for v in cells) + "</tr>")
    note = ""
    if shown_images is not None and shown_images < n:
        note = (
            f"<p><em>Photos show {shown_images} of {n} cards (eBay allows {MAX_IMAGES} "
            "images per listing); every card is listed in the table above.</em></p>"
        )
    desc = (
        f"<h2>{n}-card lot</h2>"
        f"<p>This listing is for the following {n} cards sold together as one lot:</p>"
        "<table border='1' cellpadding='4' cellspacing='0'>"
        "<tr><th>#</th><th>Year</th><th>Set</th><th>Player</th>"
        "<th>Card #</th><th>Parallel</th><th>Condition</th></tr>"
        + "".join(rows)
        + "</table>"
        + note
    )
    return desc[:MAX_DESCRIPTION]


def set_sku(cards) -> str:
    """Stable, unique, short (<50 char) SKU for a lot, derived from its card ids."""
    ids = sorted(c.id for c in cards)
    digest = hashlib.sha1(",".join(str(i) for i in ids).encode()).hexdigest()[:12]
    return f"SET-{ids[0]}-{len(ids)}-{digest}"


def set_image_urls(cards, base: str, limit: int = MAX_IMAGES) -> list[str]:
    """Combine each card's crop into one image list, capped at eBay's max (24)."""
    urls = []
    for c in cards:
        u = card_image_url(c, base)
        if u:
            urls.append(u)
        if len(urls) >= limit:
            break
    return urls


def set_image_paths(cards, limit: int = MAX_IMAGES) -> list[str]:
    """Local front-crop files for a lot, capped at eBay's max (24)."""
    return [c.crop_path for c in cards if getattr(c, "crop_path", None)][:limit]
