"""Application configuration, loaded from environment / .env."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root = parent of the `app` package.
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
CROPS_DIR = DATA_DIR / "crops"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Vision provider for card identification: auto | anthropic | gemini.
    # "auto" picks whichever key is set (Anthropic preferred if both).
    vision_provider: str = "auto"
    # Anthropic
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-8"
    # Google Gemini (alternative vision provider)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    # A higher-quality Gemini model used for on-demand "re-analyze" of a single
    # low-confidence card (slower/pricier than the default flash model).
    gemini_model_hq: str = "gemini-2.5-pro"

    # eBay listing mode:
    #   preview = build the real listing payload but DO NOT send it (no creds
    #             needed; nothing is published — clearly labeled as a preview)
    #   sandbox = publish to eBay's sandbox
    #   live    = publish to real eBay
    # Pricing ALWAYS uses real sold data regardless of this setting.
    ebay_mode: str = "preview"  # preview | sandbox | live
    ebay_client_id: str = ""
    ebay_client_secret: str = ""
    ebay_user_refresh_token: str = ""
    ebay_marketplace_id: str = "EBAY_US"
    ebay_fulfillment_policy_id: str = ""
    ebay_payment_policy_id: str = ""
    ebay_return_policy_id: str = ""
    ebay_merchant_location_key: str = ""
    # Leaf category for the listing. Default 261328 = Baseball Cards.
    ebay_category_id: str = "261328"
    # Default eBay item condition enum for raw (ungraded) cards.
    ebay_condition: str = "USED_VERY_GOOD"
    # Public base URL where saved crops are reachable by eBay (required for live
    # listings, which must include at least one image URL). e.g. https://my.host
    public_image_base_url: str = ""
    # Enable the Marketplace Insights API (real SOLD prices). Turn on only after
    # eBay grants your app the buy.marketplace.insights scope.
    ebay_insights_enabled: bool = False
    # Enable per-card headless-browser scraping of eBay sold pages (best-effort,
    # ToS-gray; requires `playwright install chromium`). Off by default.
    ebay_browser_scrape_enabled: bool = False
    # PriceCharting account token — works for SportsCardsPro (sports cards).
    pricecharting_token: str = ""
    # API base. SportsCardsPro covers sports cards; pricecharting.com covers
    # games/Funko/Marvel/etc. For baseball cards keep the SportsCardsPro base.
    cardpricing_api_base: str = "https://www.sportscardspro.com"
    # Preferred SOLD-price source. If comps from this source exist they drive the
    # "Last sold" estimate; other sold sources are used only as a fallback.
    # Match is by source-name prefix, e.g. "sportscardspro", "ebay". Blank = pool all.
    primary_sold_source: str = "sportscardspro"

    # Business rules
    min_store_value: float = 4.0
    confidence_threshold: float = 0.7
    price_markup: float = 1.5
    max_cards: int = 9
    verify_identification: bool = True
    comp_recency_days: int = 90
    min_exact_comps: int = 3

    # Optional web-search fallback
    websearch_api_key: str = ""

    # Storage
    database_url: str = f"sqlite:///{DATA_DIR / 'cards.db'}"


@lru_cache
def get_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CROPS_DIR.mkdir(parents=True, exist_ok=True)
    return Settings()
