"""Progress of background jobs (uploads, price refreshes) and retries."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Job
from app.services import jobs

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("/active")
def active(db: Session = Depends(get_db)) -> dict:
    """Unfinished jobs, plus finished ones from the last day that still have
    failed photos (until dismissed), so a page refresh can resume showing them.

    Declared above /{job_id} so "active" is not read as a job id."""
    return {"jobs": [jobs.serialize(j) for j in jobs.active_jobs(db)]}


@router.get("/{job_id}")
def get_job(job_id: str, db: Session = Depends(get_db)) -> dict:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return jobs.serialize(job)


@router.post("/{job_id}/retry/{photo_index}")
def retry(
    job_id: str,
    photo_index: int,
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> dict:
    """Queue a failed photo (or card) again. A photo skipped as a repeat
    upload is processed anyway with force=true."""
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        jobs.retry(db, job, photo_index, force=force)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except jobs.RetryRefused as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    jobs.kick()
    db.expire_all()
    return jobs.serialize(db.get(Job, job_id))


@router.post("/{job_id}/dismiss")
def dismiss(job_id: str, db: Session = Depends(get_db)) -> dict:
    """Hide a finished job's failures from /api/jobs/active."""
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    job.dismissed = True
    db.commit()
    return jobs.serialize(job)
