"""Price-source health: is each comp source answering?

GET /api/sources/health lets the UI show a banner such as "SportsCardsPro
rejected the API token" instead of every card silently reading "no price".
Shape (see comp_sources.source_health):

    {
      "sources": [
        {"source": "sportscardspro", "label": "SportsCardsPro",
         "state": "auth_expired", "ok": false, "message": "...", "count": 0,
         "last_checked_at": "2026-10-04T12:00:00+00:00",
         "last_success_at": null | "...", "last_error": "...",
         "last_error_at": "..."}
      ],
      "problems": [ ...entries above with ok == false... ],
      "banner": "SportsCardsPro: ..." | null
    }

state is one of ok, empty, error, auth_expired, unauthorized, blocked, quota.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import comp_sources

router = APIRouter(prefix="/api/sources", tags=["sources"])


@router.get("/health")
def sources_health(db: Session = Depends(get_db)) -> dict:
    return comp_sources.source_health(db)
