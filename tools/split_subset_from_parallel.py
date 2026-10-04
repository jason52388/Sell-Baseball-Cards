"""Move known subset / insert names out of `parallel` into the new `subset` field.

Before `subset` existed, the detection prompt told the model to put inserts
and subsets ("League Leaders", "Record Breaker") in `parallel`, next to finish
variants ("Gold /99", "Refractor"). Pricing treats a parallel as a must-match
word, so a subset name there makes the card look like a rarer variant. This
tool splits such values: the subset name moves to `subset`, any finish left
over stays in `parallel`. A card that already has a subset is left alone.

Dry run by default (prints the plan). `--apply` writes the changes.

Usage:
  python -m tools.split_subset_from_parallel --data-dir data
  python -m tools.split_subset_from_parallel --data-dir data --apply
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import Card

# Subset / insert banners seen on base-set cards. Longest first so "All-Star
# Rookie" wins over "All-Star".
KNOWN_SUBSETS = sorted(
    [
        "League Leaders", "Magic Moments", "Record Breaker", "Record Breakers",
        "All-Star", "All Star", "All-Star Rookie", "Highlights", "Season Highlights",
        "Postseason Highlights", "World Series", "Turn Back the Clock",
        "Future Stars", "Star Rookie", "Rated Rookie", "Draft Pick", "Prospects",
        "Team Leaders", "Golden Moments", "Special Report", "Checklist",
        "Diamond Kings", "Super Veteran", "In Action", "Award Winners",
        "Gold Glove",
    ],
    key=len,
    reverse=True,
)

_SEPARATORS = " ,;/-|"

# Words that describe a finish or numbering variant: these are a real parallel
# and stay in `parallel`. Everything else in a value that names a subset is part
# of the subset's name ("Career Highlights", "Checklist #2").
_FINISH_RE = re.compile(
    r"(?<![A-Za-z])(gold|silver|bronze|platinum|refractor|x-?fractor|foil|holo"
    r"|holographic|prizm|chrome|black|red|blue|green|orange|purple|pink|sapphire"
    r"|atomic|rainbow|mirror|wave|die-cut|parallel|numbered)(?![A-Za-z])"
    r"|/\d+",
    re.I,
)
# Labels the old prompt appended to say "this is a subset", not part of a name.
_LABEL_RE = re.compile(
    r"\(\s*(?:insert/subset|subset|insert)\s*\)|\b(?:insert/subset|subset|insert)\b", re.I)


def _canonical(name: str) -> str:
    for known in KNOWN_SUBSETS:
        if known.lower() == name.lower():
            return known
    return name


def _tidy(text: str) -> str:
    text = re.sub(r"\(\s*\)", " ", text)          # parentheses emptied by a removal
    text = re.sub(r"\(\s*([^()]*?)\s*\)", r"(\1)", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(_SEPARATORS + " ")


def split_parallel(parallel: str | None) -> tuple[str | None, str | None]:
    """(parallel left over, subset found) for one stored parallel value.

    A value that names a known subset, or is labelled "subset"/"insert", is a
    subset. Its finish words (Gold, Refractor, /99) stay in `parallel`; every
    other word stays with the subset, so "Career Highlights" never splits into
    parallel "Career" and subset "Highlights".
    """
    if not parallel:
        return parallel, None
    known = any(
        re.search(rf"(?<![A-Za-z]){re.escape(k)}(?![A-Za-z])", parallel, re.I)
        for k in KNOWN_SUBSETS
    )
    if not known and not _LABEL_RE.search(parallel):
        return parallel, None
    finish = " ".join(m.group() for m in _FINISH_RE.finditer(parallel)) or None
    subset = _tidy(_LABEL_RE.sub(" ", _FINISH_RE.sub(" ", parallel)))
    if not subset:
        return parallel, None
    return finish, _canonical(subset)


def run(db: Session, apply: bool = False) -> list[dict]:
    """Plan (and with apply=True, write) the split for every card. Returns the
    changes as dicts: card_id, player, old parallel, new parallel, subset."""
    plan: list[dict] = []
    for card in db.query(Card).filter(Card.parallel.isnot(None)).order_by(Card.id):
        if card.subset:
            continue
        rest, subset = split_parallel(card.parallel)
        if not subset:
            continue
        plan.append({
            "card_id": card.id, "player": card.player, "old_parallel": card.parallel,
            "parallel": rest, "subset": subset,
        })
        if apply:
            card.parallel, card.subset = rest, subset
    if apply:
        db.commit()
    return plan


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", type=Path, default=Path("data"))
    ap.add_argument("--apply", action="store_true", help="write the changes")
    args = ap.parse_args()

    from tools._db import open_session

    db = open_session(args.data_dir)
    plan = run(db, apply=args.apply)
    for p in plan:
        print(f"card {p['card_id']:>5}  {p['player'] or '?':<28} "
              f"parallel {p['old_parallel']!r} -> {p['parallel']!r}, subset {p['subset']!r}")
    verb = "updated" if args.apply else "would update (dry run; add --apply)"
    print(f"{len(plan)} card(s) {verb}")


if __name__ == "__main__":
    main()
