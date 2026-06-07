"""Application configuration, loaded from environment / .env."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root = parent of the `app` package.
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
CROPS_DIR = DATA_DIR / "crops"
# Photos dropped on the website are queued here (no AI call) for the Claude
# subscription loop (tools/ingest_folder.sh) to identify into the repository.
INBOX_DIR = DATA_DIR / "inbox"
INBOX_PROCESSED_DIR = INBOX_DIR / "processed"
# Locally-saved marketplace reference photos (so the UI doesn't hot-link eBay).
REF_IMAGES_DIR = DATA_DIR / "ref_images"


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
    # eBay RuName (redirect URL name) used as the OAuth redirect_uri during the
    # one-time user-consent flow that mints EBAY_USER_REFRESH_TOKEN. Created in
    # the eBay portal under your keyset's "User tokens / Get a Token ... via Your
    # Application" → add a redirect URL whose "auth accepted URL" points at
    # <public URL>/ebay/oauth/callback. Looks like: Jason_Man-JasonMan-SellBa-xxxx
    ebay_ru_name: str = ""
    # Marketplace Account Deletion notification (REQUIRED to activate production
    # keys). The verification token is any 32-80 char string of [A-Za-z0-9_-]
    # that you also paste into the eBay portal. The endpoint URL must EXACTLY
    # match the public HTTPS URL registered there (used in the challenge hash),
    # e.g. https://abc123.ngrok-free.app/ebay/account-deletion
    ebay_verification_token: str = ""
    ebay_deletion_endpoint_url: str = ""
    ebay_marketplace_id: str = "EBAY_US"
    ebay_fulfillment_policy_id: str = ""
    ebay_payment_policy_id: str = ""
    ebay_return_policy_id: str = ""
    ebay_merchant_location_key: str = ""
    # Leaf category for the listing. Default 261328 = Baseball Cards.
    ebay_category_id: str = "261328"
    # Leaf category for SET / lot listings. 261329 = Sports Trading Card Lots
    # (eBay expects a "Number of Cards" item specific there, which we send).
    ebay_lot_category_id: str = "261329"
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
    # Enable 130point.com sold-comp lookups. Adds recent sales INCLUDING the real
    # best-offer-accepted prices eBay hides on public completed listings.
    # Best-effort scrape (no official API), ToS-gray. Off by default.
    point130_enabled: bool = False
    # PriceCharting account token — works for SportsCardsPro (sports cards).
    pricecharting_token: str = ""
    # API base. SportsCardsPro covers sports cards; pricecharting.com covers
    # games/Funko/Marvel/etc. For baseball cards keep the SportsCardsPro base.
    cardpricing_api_base: str = "https://www.sportscardspro.com"
    # Scrape the SportsCardsPro product page's recent-sales table for INDIVIDUAL
    # dated sales (the API only returns aggregate prices). No official API,
    # ToS-gray. Off by default. Verify markup with tools/verify_sportscardspro.py.
    sportscardspro_sales_enabled: bool = False
    # Preferred SOLD-price source. If comps from this source exist they drive the
    # "Last sold" estimate; other sold sources are used only as a fallback.
    # Match is by source-name prefix, e.g. "sportscardspro", "ebay". Blank = pool all.
    primary_sold_source: str = "sportscardspro"

    # Business rules
    min_store_value: float = 4.0
    confidence_threshold: float = 0.7
    price_markup: float = 1.5
    max_cards: int = 9
    # Safety margin added around each detected card box before cropping (fraction
    # of the box's size, per side). The vision model's boxes often shave a card
    # edge; this pads them so the whole card is captured. ~8% works well.
    crop_padding_pct: float = 0.08
    # After the padded crop, detect the card's actual rectangle (OpenCV) and warp
    # it straight — removes leftover background and deskews tilted cards. Falls
    # back to the padded crop when no clean card rectangle is found.
    crop_autostraighten: bool = True
    # Selling-cost assumptions for the collection KPIs (what it costs you to sell).
    # eBay trading-card final-value fee (~13.25%) + per-order fee, plus the
    # supplies to ship one card (penny sleeve + top-loader + mailer + label).
    ebay_fee_pct: float = 0.1325
    ebay_per_order_fee: float = 0.40
    supplies_cost_per_card: float = 0.60
    verify_identification: bool = True
    comp_recency_days: int = 90
    min_exact_comps: int = 3

    # Optional web-search fallback
    websearch_api_key: str = ""

    # --- Caching (reduce API load) ---
    # How long a cached set of comps for a card identity is reused before
    # re-querying the price APIs. Sold/market prices move slowly, so this can be
    # long. 0 disables the persistent cache entirely. Default is effectively
    # "never expire" (~100 years) — use the Refresh prices button to force a
    # fresh fetch on demand.
    price_cache_ttl_days: int = 36525
    # How long an accumulated (dated) sold comp is retained as price history when
    # a card's cached comps are refreshed. Dated sales older than this are pruned;
    # 0 keeps history forever. Active/undated comps are never accumulated.
    price_history_retention_days: int = 365
    # Download the chosen marketplace reference photo into data/ref_images and
    # serve it locally, instead of hot-linking eBay's CDN (which rotates URLs).
    localize_reference_images: bool = True

    # When set, a card's ORIGINAL source photo(s) are MOVED into this folder once
    # the card is added to the collection (archival; e.g. an iCloud Drive folder).
    # Crops are independent copies, so moving the source is safe. Blank = disabled.
    collection_photos_dir: str = ""

    # Storage
    database_url: str = f"sqlite:///{DATA_DIR / 'cards.db'}"


@lru_cache
def get_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CROPS_DIR.mkdir(parents=True, exist_ok=True)
    INBOX_PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    REF_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    return Settings()
