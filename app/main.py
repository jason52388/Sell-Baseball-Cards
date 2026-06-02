"""FastAPI application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import CROPS_DIR, get_settings
from app.db import init_db
from app.routers import cards, listings, upload

logging.basicConfig(level=logging.INFO)

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Sell Baseball Cards", lifespan=lifespan)

app.include_router(upload.router)
app.include_router(cards.router)
app.include_router(listings.router)

# Serve saved card crops.
app.mount("/crops", StaticFiles(directory=CROPS_DIR), name="crops")


@app.get("/api/config")
def config() -> dict:
    s = get_settings()
    return {
        "ebay_mode": s.ebay_mode,
        "price_markup": s.price_markup,
        "min_store_value": s.min_store_value,
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/repository")
def repository() -> FileResponse:
    return FileResponse(STATIC_DIR / "repository.html")


@app.get("/card/{card_id}")
def card_detail(card_id: int) -> FileResponse:
    return FileResponse(STATIC_DIR / "card.html")


# Static assets (js/css) under /static.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
