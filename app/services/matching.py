"""Score sold comps against a card identity and partition exact/near/graded.

A comp is judged by how many identity tokens (player, year, set, card number)
appear in its title. Exact matches drive the price; near matches are same-ish
cards (e.g. different grade); graded comps are tagged separately.

Before any of that, a comp is EXCLUDED (with a reason) when it is not a sale of
this card at all: junk listings (lots, reprints, customs, digital, breaks), a
different player, or a parallel the card does not have.

This module also owns the single grade pattern every source uses (GRADE_RE).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.ebay.base import SoldComp


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())


def _has_word(title_norm: str, word: str) -> bool:
    """Whole-word containment. Substring tests would match "bo" inside "bob"
    and the year "1989" inside "219890"."""
    return re.search(rf"\b{re.escape(word)}\b", title_norm) is not None


# --- Grades -------------------------------------------------------------------
# The ONE grade pattern for every source (matching, SportsCardsPro, 130point,
# the eBay scrapers/APIs). A grade missed here is counted as a RAW sale, and slab
# prices are many times the raw price, so it accepts the grade words sellers put
# between the grader and the number ("PSA Gem Mint 10", "BGS Pristine 10",
# "PSA NM-MT 8") while refusing autograph-authentication-only tags ("PSA/DNA").
_GRADERS = r"psa|bgs|bvg|sgc|csg|cgc"
_GRADE_WORDS = r"gem|mint|mt|nm|near|ex|vg|pristine|black|gold|label|perfect|grade|graded"
GRADE_RE = re.compile(
    rf"\b({_GRADERS})(?![a-z/])"
    rf"(?:[\s:#\-]*(?:{_GRADE_WORDS})\b)*"
    r"[\s:#\-]*(10|[1-9](?:\.5)?)(?!\d|\.\d|/)",
    re.IGNORECASE,
)
# Back-compat alias for older imports.
_GRADE_RE = GRADE_RE


def detect_grade(text: str | None) -> str | None:
    """Normalized grade ("PSA 10") named in a title, or None."""
    if not text:
        return None
    m = GRADE_RE.search(text)
    return f"{m.group(1).upper()} {m.group(2)}" if m else None


def is_graded(comp: SoldComp) -> bool:
    """A slab sale: the title or condition names a grade, or the marketplace
    reports the condition as simply "Graded" (eBay Browse does this)."""
    if GRADE_RE.search(comp.title or ""):
        return True
    cond = (comp.condition_grade or "").strip()
    if not cond:
        return False
    return cond.lower() == "graded" or bool(GRADE_RE.search(cond))


# --- Parallel markers -----------------------------------------------------------
# A base card sells for a fraction of its parallels, so a sale naming a parallel
# the card does not have must never price it. Whole-word only ("Gold" is not in
# "Goldschmidt"), and a marker that is part of the card's own set, parallel,
# subset or player name is allowed ("Topps Gold Label", "Topps Chrome").
PARALLEL_WORDS = frozenset({
    "gold", "refractor", "refractors", "prizm", "holo", "foil", "xfractor",
    "atomic", "sapphire", "chrome", "superfractor", "mojo", "shimmer", "speckle",
    "rainbow", "black", "parallel", "variation", "sp", "ssp",
})
PARALLEL_PHRASES = ("printing plate", "short print", "cracked ice", "serial numbered")
# Serial numbering ("/50", "#/99", "23/50", "1/1").
_SERIAL_RE = re.compile(r"(?:^|[\s#(])/\s?\d{1,4}\b|\b\d{1,4}\s?/\s?\d{1,4}\b")
# Phrases that contain a marker word without naming a parallel.
_MARKER_FALSE_FRIENDS = re.compile(r"\bgold\s+gloves?\b", re.IGNORECASE)


def parallel_markers(
    title: str | None,
    allowed_words: set[str] | frozenset[str] = frozenset(),
    *,
    allow_serial: bool = False,
) -> list[str]:
    """Parallel markers named in `title` that are not in `allowed_words`."""
    # Grade text ("BGS Black Label 10") is not a parallel.
    raw = GRADE_RE.sub(" ", (title or "").lower())
    raw = _MARKER_FALSE_FRIENDS.sub(" ", raw)
    norm = _norm(raw)
    words = set(norm.split())
    found = sorted(w for w in (words & PARALLEL_WORDS) if w not in allowed_words)
    for phrase in PARALLEL_PHRASES:
        if _has_word(norm, phrase) and not set(phrase.split()) <= set(allowed_words):
            found.append(phrase)
    if not allow_serial and _SERIAL_RE.search(raw):
        found.append("serial-numbered")
    return found


# --- Junk listings ----------------------------------------------------------------
# Lots, reprints, customs, digital cards and breaks are not a sale of this card.
_JUNK_WORDS = frozenset({
    "lot", "lots", "bundle", "choose", "reprint", "reprints", "rp", "custom",
    "customs", "aceo", "novelty", "digital", "nft", "facsimile", "replica",
    "break", "breaks",
})
_JUNK_PHRASES = (
    "lot of", "you pick", "pick your", "u pick", "re print", "art card",
    "topps bunt", "case break",
)
_JUNK_RAW = [
    (re.compile(r"\bx\s?\d{1,3}\b", re.IGNORECASE), "quantity (xN)"),
    (re.compile(r"\((?:[2-9]|\d{2,3})\)"), "quantity (N)"),
]


def junk_reason(title: str | None) -> str | None:
    """Why a listing is not a single genuine card, or None."""
    raw = title or ""
    norm = _norm(raw)
    for phrase in _JUNK_PHRASES:
        if _has_word(norm, phrase):
            return phrase
    hit = sorted(set(norm.split()) & _JUNK_WORDS)
    if hit:
        return hit[0]
    for rx, label in _JUNK_RAW:
        if rx.search(raw):
            return label
    return None


# --- Player name ------------------------------------------------------------------
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def player_last_name(player: str | None) -> str | None:
    """Normalized surname ("Ken Griffey Jr." -> "griffey"), or None."""
    words = [w for w in _norm(player).split() if w not in _NAME_SUFFIXES]
    return words[-1] if words else None


def _significant_words(token: str) -> list[str]:
    """Words worth matching on. Falls back to the whole token when every word is
    too short to be distinctive (e.g. a set named "SP")."""
    words = [w for w in token.split() if len(w) > 2]
    return words or ([token] if token else [])


@dataclass
class ScoredComp:
    comp: SoldComp
    match_type: str  # exact | near | graded | excluded
    match_reason: str


def _identity_tokens(card) -> dict[str, str]:
    """Map of field -> normalized token we expect to see in a matching title."""
    tokens: dict[str, str] = {}
    if getattr(card, "player", None):
        tokens["player"] = _norm(card.player).strip()
    if getattr(card, "year", None):
        tokens["year"] = _norm(card.year).strip()
    if getattr(card, "set_brand", None):
        tokens["set"] = _norm(card.set_brand).strip()
    if getattr(card, "card_number", None):
        tokens["number"] = _norm(str(card.card_number)).strip()
    if getattr(card, "parallel", None):
        tokens["parallel"] = _norm(card.parallel).strip()
    if getattr(card, "subset", None):
        tokens["subset"] = _norm(card.subset).strip()
    return tokens


def card_allowed_marker_words(card) -> set[str]:
    """Words that may appear in a comp title without implying a parallel the
    card lacks: its own set, parallel, subset and player name."""
    allowed: set[str] = set()
    for field in ("set_brand", "parallel", "subset", "player"):
        allowed |= set(_norm(getattr(card, field, None)).split())
    return allowed


def _player_tokens_present(title_norm: str, player_norm: str) -> bool:
    """All words of the player's name must appear in the title."""
    if not player_norm:
        return False
    return all(_has_word(title_norm, w) for w in player_norm.split() if w)


def score_comp(card, comp: SoldComp) -> ScoredComp:
    title_norm = _norm(comp.title)
    tokens = _identity_tokens(card)

    junk = junk_reason(comp.title)
    if junk:
        return ScoredComp(comp, "excluded", f"junk listing ({junk})")

    player_ok = _player_tokens_present(title_norm, tokens.get("player", ""))
    if not player_ok:
        return ScoredComp(comp, "excluded", "player not found in title")

    markers = parallel_markers(
        comp.title,
        card_allowed_marker_words(card),
        allow_serial=bool(getattr(card, "parallel", None) or getattr(card, "serial_number", None)),
    )
    if markers:
        what = "card is base" if not tokens.get("parallel") else "card is a different parallel"
        return ScoredComp(
            comp, "excluded", f"parallel in title ({', '.join(markers)}); {what}"
        )

    matched = ["player"]
    if tokens.get("year") and _has_word(title_norm, tokens["year"]):
        matched.append("year")
    if tokens.get("set"):
        # Every significant word must appear: "Topps" alone is a different (and
        # differently priced) product from "Topps Chrome".
        if all(_has_word(title_norm, w) for w in _significant_words(tokens["set"])):
            matched.append("set")
    if tokens.get("number") and re.search(
        rf"#?\b{re.escape(tokens['number'])}\b", title_norm
    ):
        matched.append("number")

    reason = "matched: " + ", ".join(matched)
    # A subset/insert name ("League Leaders") is a supporting signal only: sellers
    # often leave it out, so it is never required.
    if tokens.get("subset") and all(
        _has_word(title_norm, w) for w in _significant_words(tokens["subset"])
    ):
        reason += ", subset"

    if is_graded(comp):
        return ScoredComp(comp, "graded", reason + " (graded)")

    # A parallel/insert sells for a multiple of its base card, so a sale can only
    # be an exact comp for one when the title names that parallel too.
    parallel_ok = True
    if tokens.get("parallel"):
        parallel_ok = all(
            _has_word(title_norm, w) for w in _significant_words(tokens["parallel"])
        )
        if parallel_ok:
            reason += ", parallel"

    # Exact requires player + at least two of year/set/number.
    strong = len(matched) - 1  # exclude the mandatory player
    if strong >= 2 and parallel_ok:
        return ScoredComp(comp, "exact", reason)
    if not parallel_ok:
        return ScoredComp(comp, "near", reason + " (parallel not in title)")
    if strong >= 1:
        return ScoredComp(comp, "near", reason)
    return ScoredComp(comp, "near", reason + " (weak)")


def partition(card, comps: list[SoldComp]) -> list[ScoredComp]:
    return [score_comp(card, c) for c in comps]
