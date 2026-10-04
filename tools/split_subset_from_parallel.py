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


def _canonical(name: str) -> str:
    for known in KNOWN_SUBSETS:
        if known.lower() == name.lower():
            return known
    return name


def split_parallel(parallel: str | None) -> tuple[str | None, str | None]:
    """(parallel left over, subset found) for one stored parallel value."""
    if not parallel:
        return parallel, None
    for known in KNOWN_SUBSETS:
        m = re.search(rf"(?<![A-Za-z]){re.escape(known)}(?![A-Za-z])", parallel, re.I)
        if not m:
            continue
        rest = (parallel[: m.start()] + " " + parallel[m.end():]).strip(_SEPARATORS + " ")
        rest = re.sub(r"\s{2,}", " ", rest).strip(_SEPARATORS) or None
        # A serial "/99" fragment keeps its slash; strip only outer separators.
        return rest, _canonical(m.group())
    return parallel, None


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
