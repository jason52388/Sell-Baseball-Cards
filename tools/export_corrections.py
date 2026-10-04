"""Export hand corrections of card identities as a JSONL golden set.

Every time an identity field is edited in the app (PATCH /api/cards/{id}), the
model's original read, the value before the edit and the final value are
stored as an IdentificationCorrection. This tool writes them out, one JSON
object per line, oldest first, so identification changes can be measured
against real mistakes. Read-only: nothing in the database changes.

Usage:
  python -m tools.export_corrections --data-dir data > corrections.jsonl
  python -m tools.export_corrections --data-dir data --out corrections.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import IdentificationCorrection


def run(db: Session) -> list[dict]:
    rows = db.query(IdentificationCorrection).order_by(IdentificationCorrection.id).all()
    return [
        {
            "card_id": r.card_id,
            "field": r.field,
            "model_value": r.model_value,
            "previous_value": r.previous_value,
            "final_value": r.final_value,
            "crop_path": r.crop_path,
            "back_crop_path": r.back_crop_path,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", type=Path, default=Path("data"))
    ap.add_argument("--out", type=Path, help="write here instead of standard output")
    args = ap.parse_args()

    from tools._db import open_session

    rows = run(open_session(args.data_dir))
    lines = "".join(json.dumps(r) + "\n" for r in rows)
    if args.out:
        args.out.write_text(lines, encoding="utf-8")
        print(f"{len(rows)} correction(s) written to {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(lines)


if __name__ == "__main__":
    main()
