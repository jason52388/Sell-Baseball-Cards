"""FastAPI application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import CROPS_DIR, REF_IMAGES_DIR, get_settings
from app.db import init_db
from app.routers import (
    cards,
    ebay_notifications,
    ebay_oauth,
    jobs as jobs_router,
    listings,
    review,
    sources,
    upload,
)
from app.services import jobs, trash

logging.basicConfig(level=logging.INFO)

STATIC_DIR = Path(__file__).resolve().parent / "static"


logger = logging.getLogger("main")


def startup_tasks() -> None:
    """Resume background work and empty the trash. Each step is best-effort:
    a failure is logged and never stops the app from starting."""
    try:
        jobs.recover()  # an item cut off mid-way by the restart -> failed, retryable
    except Exception:  # noqa: BLE001
        logger.exception("job recovery failed")
    try:
        with jobs.session() as db:
            trash.purge_expired(db)  # soft-deleted cards past the restore window
    except Exception:  # noqa: BLE001
        logger.exception("trash purge failed")
    try:
        upload.purge_staged_duplicates()
    except Exception:  # noqa: BLE001
        logger.exception("staged duplicate cleanup failed")
    try:
        if jobs.has_waiting():
            jobs.kick()
    except Exception:  # noqa: BLE001
        logger.exception("could not start the job worker")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    startup_tasks()
    yield
    # An item cut off here is failed by recover() on the next start (retryable).
    jobs.stop_worker(timeout=2)


app = FastAPI(title="Sell Cards", lifespan=lifespan)


@app.middleware("http")
async def no_store_assets(request, call_next):
    """Don't let the browser cache the app's HTML/CSS/JS — otherwise UI edits
    appear not to take effect until a hard refresh."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith(("/static", "/refimg", "/crops")) or path in (
        "/", "/repository", "/review", "/card",
    ) or path.startswith("/card/"):
        response.headers["Cache-Control"] = "no-store"
    return response


app.include_router(upload.router)
app.include_router(cards.router)
app.include_router(listings.router)
app.include_router(ebay_notifications.router)
app.include_router(ebay_oauth.router)
app.include_router(sources.router)
app.include_router(jobs_router.router)
app.include_router(review.router)

# Serve saved card crops.
app.mount("/crops", StaticFiles(directory=CROPS_DIR), name="crops")
# Serve locally-saved marketplace reference photos.
app.mount("/refimg", StaticFiles(directory=REF_IMAGES_DIR), name="refimg")


@app.get("/api/config")
def config() -> dict:
    """Display settings. List prices come per card (CardOut.suggested_list_price,
    the same rule the listing endpoints use); price_markup alone is NOT the list
    price (asking-based cards use ebay_ask_undercut, and the floor applies)."""
    from app.services.ebay.listing_common import listing_price_floor

    s = get_settings()
    return {
        "ebay_mode": s.ebay_mode,
        "price_markup": s.price_markup,
        "ebay_ask_undercut": s.ebay_ask_undercut,
        "price_floor": listing_price_floor(s),
        "min_store_value": s.min_store_value,
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/repository")
def repository() -> FileResponse:
    return FileResponse(STATIC_DIR / "repository.html")


@app.get("/review")
def review_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "review.html")


@app.get("/card/{card_id}")
def card_detail(card_id: int) -> FileResponse:
    return FileResponse(STATIC_DIR / "card.html")


# Static assets (js/css) under /static.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
