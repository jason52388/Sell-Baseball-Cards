"""Background jobs: uploads and price refreshes, one item at a time.

A photo with several cards takes minutes (detection, a close-up re-read per
card, verification, pricing). Doing that inside the HTTP request held the
request open with no progress and held SQLite's write lock throughout. Now:

- the request saves its input and records a Job with one JobItem per photo
  (or per card, for a price refresh), then returns the job id at once;
- ONE worker thread runs waiting items in order, one at a time, so the vision
  CLI and SQLite are never asked to do two at once;
- the handler commits after every step and reports progress through
  `progress("Pricing card 2 of 6")`, which the UI polls (GET /api/jobs/{id});
- job state lives in the database, so after a restart `recover()` marks an
  item that was mid-flight as failed (retryable) and the worker resumes the
  waiting ones.

Handlers are registered by kind (`register`): the upload router registers
"upload", the cards router "reprice". A handler sets `item.message` and
`item.card_ids_json`; raising `ItemFailed` fails the item with its message.

Tests set INLINE = True (and `session_factory`) so kick() runs the queue
synchronously instead of in a thread.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ITEM_DONE,
    ITEM_FAILED,
    ITEM_WAITING,
    ITEM_WORKING,
    Job,
    JobItem,
)

logger = logging.getLogger("jobs")

# None = app.db.SessionLocal. Tests point this at their own sessionmaker.
session_factory: Callable[[], Session] | None = None
# Run the queue synchronously inside kick() (tests).
INLINE = False

# A finished job with failures stays in /api/jobs/active this long, unless
# dismissed, so a page refresh still shows what needs a retry.
FAILED_VISIBLE_FOR = timedelta(hours=24)
INTERRUPTED = "Interrupted by a server restart. Retry to process it again."

Handler = Callable[[Session, Job, JobItem, Callable[[str], None]], None]
RetryHook = Callable[[Session, Job, JobItem, bool], None]
_handlers: dict[str, Handler] = {}
_retry_hooks: dict[str, RetryHook] = {}

_wake = threading.Event()
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()
_run_lock = threading.Lock()


class ItemFailed(Exception):
    """Fail the current item with this message (no traceback logged)."""


class RetryRefused(Exception):
    """The item cannot be retried as asked."""


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _factory() -> Callable[[], Session]:
    if session_factory is not None:
        return session_factory
    from app.db import SessionLocal

    return SessionLocal


def session() -> Session:
    """A new session from the job session factory (use as a context manager)."""
    return _factory()()


def register(kind: str, handler: Handler, retry_hook: RetryHook | None = None) -> None:
    _handlers[kind] = handler
    if retry_hook is not None:
        _retry_hooks[kind] = retry_hook


# --- creating jobs -------------------------------------------------------------


def new_job(db: Session, kind: str, params: dict | None = None) -> Job:
    job = Job(id=uuid.uuid4().hex, kind=kind, params_json=json.dumps(params or {}))
    db.add(job)
    return job


def add_item(db: Session, job: Job, filename: str, **fields) -> JobItem:
    fields.setdefault("state", ITEM_WAITING)  # set now: column defaults wait for a flush
    item = JobItem(job=job, idx=len(job.items), filename=filename[:512], **fields)
    db.add(item)
    return item


def params(job: Job) -> dict:
    try:
        out = json.loads(job.params_json or "{}")
    except Exception:  # noqa: BLE001
        out = {}
    return out if isinstance(out, dict) else {}


def set_card_ids(item: JobItem, ids) -> None:
    item.card_ids_json = json.dumps([int(i) for i in ids])


def card_ids(item: JobItem) -> list[int]:
    try:
        out = json.loads(item.card_ids_json or "[]")
    except Exception:  # noqa: BLE001
        return []
    return out if isinstance(out, list) else []


# --- running -------------------------------------------------------------------


def _finish_if_done(job: Job) -> None:
    if all(i.state in (ITEM_DONE, ITEM_FAILED) for i in job.items):
        job.finished_at = job.finished_at or now()
    else:
        job.finished_at = None


def run_one() -> bool:
    """Run the oldest waiting item. Returns False when nothing is waiting."""
    with _run_lock, _factory()() as db:
        item = db.scalars(
            select(JobItem).join(Job)
            .where(JobItem.state == ITEM_WAITING)
            .order_by(Job.created_at, JobItem.job_id, JobItem.idx)
            .limit(1)
        ).first()
        if item is None:
            return False
        item_id = item.id
        job = item.job
        item.state = ITEM_WORKING
        item.step = "Starting"
        item.message = None
        item.updated_at = now()
        db.commit()

        def progress(step: str) -> None:
            item.step = step[:255]
            item.updated_at = now()
            db.commit()

        handler = _handlers.get(job.kind)
        try:
            if handler is None:
                raise ItemFailed(f"no handler for job kind {job.kind!r}")
            handler(db, job, item, progress)
            state, message = ITEM_DONE, item.message
        except ItemFailed as exc:
            db.rollback()
            state, message = ITEM_FAILED, str(exc)
        except Exception as exc:  # noqa: BLE001 - one bad item never stops the queue
            logger.exception("job %s item %s failed", job.id, item_id)
            db.rollback()
            state, message = ITEM_FAILED, f"Failed: {exc}"[:2000]
        item = db.get(JobItem, item_id)
        item.state = state
        item.message = message
        item.step = None
        item.updated_at = now()
        _finish_if_done(item.job)
        db.commit()
        return True


def run_pending(limit: int | None = None) -> int:
    n = 0
    while (limit is None or n < limit) and run_one():
        n += 1
    return n


_stop = threading.Event()


def _loop() -> None:
    while not _stop.is_set():
        try:
            worked = run_one()
        except Exception:  # noqa: BLE001
            logger.exception("job worker error")
            worked = False
            time.sleep(1)
        if not worked:
            _wake.wait(timeout=30)
            _wake.clear()


def kick() -> None:
    """Wake the worker (starting it if needed)."""
    global _thread
    if INLINE:
        run_pending()
        return
    with _thread_lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, name="job-worker", daemon=True)
            _thread.start()
    _wake.set()


def stop_worker(timeout: float = 5.0) -> None:
    """Stop the worker thread after its current item (tests, shutdown)."""
    global _thread
    with _thread_lock:
        thread = _thread
        _thread = None
    if thread is None:
        return
    _stop.set()
    _wake.set()
    thread.join(timeout)
    _stop.clear()
    _wake.clear()


# --- startup -------------------------------------------------------------------


def recover() -> int:
    """After a restart: an item left 'working' was cut off mid-way, so it is
    failed with a retry note; waiting items are left for the worker."""
    with _factory()() as db:
        stuck = list(db.scalars(select(JobItem).where(JobItem.state == ITEM_WORKING)))
        for item in stuck:
            item.state = ITEM_FAILED
            item.step = None
            item.message = INTERRUPTED
            item.updated_at = now()
        for job in db.scalars(select(Job).where(Job.finished_at.is_(None))):
            _finish_if_done(job)
        db.commit()
        return len(stuck)


def has_waiting() -> bool:
    with _factory()() as db:
        return db.scalars(
            select(JobItem.id).where(JobItem.state == ITEM_WAITING).limit(1)
        ).first() is not None


# --- retry / dismiss -----------------------------------------------------------


def retry(db: Session, job: Job, idx: int, force: bool = False) -> JobItem:
    item = next((i for i in job.items if i.idx == idx), None)
    if item is None:
        raise LookupError("no such photo in this job")
    if item.state in (ITEM_WAITING, ITEM_WORKING):
        raise RetryRefused("this photo is still being processed")
    if item.state == ITEM_DONE and not (item.duplicate and force):
        raise RetryRefused(
            "only a failed photo can be retried (or a skipped repeat upload, with force=true)"
        )
    hook = _retry_hooks.get(job.kind)
    if hook is not None:
        hook(db, job, item, force)
    item.state = ITEM_WAITING
    item.step = None
    item.message = None
    item.updated_at = now()
    job.finished_at = None
    job.dismissed = False
    db.commit()
    return item


# --- reading -------------------------------------------------------------------


def status(job: Job) -> str:
    states = [i.state for i in job.items]
    if not states or all(s in (ITEM_DONE, ITEM_FAILED) for s in states):
        return "done"
    if all(s == ITEM_WAITING for s in states):
        return "queued"
    return "running"


def serialize(job: Job) -> dict:
    p = params(job)
    photos = [
        {
            "index": i.idx,
            "filename": i.filename,
            "state": i.state,
            "step": i.step,
            "message": i.message,
            "card_ids": card_ids(i),
            "upload_id": i.upload_id,
            "card_id": i.card_id,
            "duplicate": bool(i.duplicate),
            "can_retry": i.state == ITEM_FAILED or bool(i.duplicate),
        }
        for i in job.items
    ]
    finished = sum(1 for i in job.items if i.state in (ITEM_DONE, ITEM_FAILED))
    return {
        "job_id": job.id,
        "kind": job.kind,
        "status": status(job),
        "batch_tag": p.get("batch_tag"),
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "total": len(job.items),
        "done": finished,
        "failed": sum(1 for i in job.items if i.state == ITEM_FAILED),
        "photos": photos,
    }


def active_jobs(db: Session) -> list[Job]:
    """Unfinished jobs, plus recently finished ones that still have failed
    items the user has not dismissed (so a refresh shows what to retry)."""
    cutoff = now() - FAILED_VISIBLE_FOR
    out = []
    for job in db.scalars(select(Job).order_by(Job.created_at)):
        if job.finished_at is None:
            out.append(job)
        elif (
            not job.dismissed
            and job.finished_at >= cutoff
            and any(i.state == ITEM_FAILED for i in job.items)
        ):
            out.append(job)
    return out
