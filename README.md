# Sell Baseball Cards

Photograph baseball cards (up to 9 per image, mass-upload many images at once),
identify and grade each one with Claude vision, price them from eBay sold comps
(+ web-search fallback), keep the valuable ones in a reviewable repository, and
create eBay Buy-It-Now listings at 50% above the estimate on demand.

## Quick start

```bash
cp .env.example .env          # add your ANTHROPIC_API_KEY
./run.sh                      # creates venv, installs deps, starts the server
```

Open http://127.0.0.1:8000 to upload, and http://127.0.0.1:8000/repository to
review and sell. eBay runs in **stub mode** by default — no eBay account needed;
listings are simulated and comps come from deterministic fixtures.

## How it works

1. **Upload** (`/api/upload`) — accepts multiple image files. Each image →
   Claude vision detects up to 9 cards (player, year, set, number, parallel,
   condition) with a **per-field confidence** and the **raw text read** off the
   card. A **second-pass verification** re-checks each crop against its proposed
   identity; disagreement lowers confidence.
2. **Grading & anomalies** — Claude estimates gem-mint potential and flags
   **PSA 10 candidates** and **valuable anomalies** (misprints, miscuts, errors).
3. **Pricing** (`app/services/pricing.py`) — builds a precise query and pulls
   real eBay comps: **last-sold** prices (Marketplace Insights) and **current
   asking** prices (Browse). It partitions matches into **exact / near / graded**,
   excludes non-matches, filters to recent sales, **trims outliers**, and takes
   the median of each. The estimate prefers sold data and falls back to asking
   prices (labeled). It captures a **reference photo** from a matched eBay
   listing. PSA 10 candidates also get a graded-value estimate. No price is ever
   invented — no data ⇒ `needs_review`.
4. **Review before adding** — detected cards land as a **preview** (status
   `preview`): persisted so the crop, comps, and reference photo are ready, but
   **not yet in your library**. The upload page shows each card next to its
   **marketplace reference photo** with a tentative estimate so you can confirm
   the match, then lets you:
   - **Add to repository** (`POST /api/cards/promote`) — runs the safeguards
     below and routes the card to `priced` / `needs_review` / `below_threshold`.
     "Add all" promotes every previewed card at once.
   - **Re-analyze with a stronger Gemini model** (`POST /api/cards/{id}/reanalyze`,
     uses `GEMINI_MODEL_HQ`, default `gemini-2.5-pro`) — re-reads the crop and
     re-prices, staying in preview. Surfaced for low-confidence cards.
   - **Discard** (`DELETE /api/cards/{id}`) — drop a previewed card.
   - **Add / correct manually** via the manual form (`POST /api/cards/manual`).
5. **Safeguards** — low confidence, incomplete identity, or no comps →
   `needs_review` (never auto-priced or auto-listed). PSA 10 / anomaly cards are
   always kept and routed to `needs_review` for human valuation.
6. **Repository / library** (`/repository`) — every **added** card is its own
   library entry (un-added previews are excluded). Browse, filter (PSA 10 /
   anomalies), see your photo next to the **marketplace reference photo**, and
   open a **card detail** page showing the identification audit, a **last-sold
   price per marketplace** summary, and **every matching sold listing (with photo
   + clickable link)**.
7. **Sell** (`/api/listings/sell`) — for selected `priced` cards, creates eBay
   Buy-It-Now listings at `estimate × 1.5`.

## Bulk identify with your Claude subscription (no API key)

Identification is the only step that needs a vision API key — pricing, cropping,
and the repository are source-agnostic. So you can identify a whole **folder of
photos with Claude Code** (billed to your Claude subscription, no
`ANTHROPIC_API_KEY` on the app) and push the results into the running site:

```bash
./run.sh                          # start the app in one terminal
tools/ingest_folder.sh ~/card-photos    # in another, with Claude Code installed
```

For each image, the script asks headless `claude -p` to read the photo and emit
the detection JSON (using the app's own `app/prompts/card_detection.py` schema),
then POSTs the image + JSON to **`POST /api/ingest`**. The server crops and prices
each card and lands it as a **preview** — you review each one next to its
marketplace reference photo and **Add** the keepers, exactly like an in-app
upload. Only the extracted identity leaves your machine; photos stay local. See
[`tools/README.md`](tools/README.md) for prerequisites and limits.

> `/api/ingest` accepts `multipart/form-data` with an `image` file and a
> `detections` field (`{"cards":[...]}` or a bare list). Anything that produces
> that schema can feed it — Claude Code is just the included driver.

## Pricing accuracy

**No fabricated data, ever.** Prices come only from real eBay data. If none is
available the card is flagged `needs_review` — a price is never invented.

The app shows **two real prices side by side** for every card (`sold_estimate`
and `active_estimate`), pulling from any combination of these real sources:

| Kind | Source | Credentials / cost |
| --- | --- | --- |
| **Current asking** | eBay **Browse API** | Free app keyset — works immediately |
| **Last sold** | eBay **Marketplace Insights API** | Free keyset **+ eBay approval** of `buy.marketplace.insights` |
| **Last sold** | **PriceCharting API** | Paid token (`PRICECHARTING_TOKEN`) — works immediately |
| **Last sold** | **Headless-browser eBay scrape** | Free; `EBAY_BROWSER_SCRAPE_ENABLED=true` + Playwright. Best-effort, ToS-gray |

The estimate prefers real **sold** data and falls back to **active asking**
prices, always labeling which basis it used (`price_basis`). Among sold sources,
the one named in `PRIMARY_SOLD_SOURCE` (default **`pricecharting`**) is preferred
— if it returns a price it drives the "Last sold" estimate, and other sold
sources (eBay Insights/scrape) are used only as a fallback. All configured
sources are still merged and shown on the card-detail page, each tagged with its
origin, so you can see exactly where every price came from.

> Note: **Marketplace Insights returns SOLD data only — never current/active
> listings.** Current "asking" prices come from the separate **Browse API**
> (needs `EBAY_CLIENT_ID/SECRET`). The two are independent.

> Plain (non-browser) scraping of eBay sold pages is **blocked by eBay (HTTP
> 403)**. The headless-browser path uses a real Chromium engine, which gets past
> most of that, but eBay can still serve a CAPTCHA and markup changes over time —
> so it's best-effort and returns nothing rather than a fake price when blocked.
> Enable it with `pip install playwright && playwright install chromium`.

### Enabling real prices
1. Create a free developer keyset at https://developer.ebay.com/my/keys and set
   `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET`. → **Current asking** prices work now.
2. In your eBay developer account, request access to the **Buy Marketplace
   Insights API**. When approved, set `EBAY_INSIGHTS_ENABLED=true`. → **Last
   sold** prices turn on. Until then the UI honestly shows asking prices only and
   explains that sold-data access is pending.

`app/services/comp_sources.py` is the single place that fans out across sources
(`insights.py` = sold, `browse.py` = active). The rest of the app (matching,
recency, outlier-trimming, UI, listing) is source-agnostic.

## Going live with eBay

Set `EBAY_MODE=sandbox` (then `live`) in `.env` and fill in:

- `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` — from your
  [eBay developer app keyset](https://developer.ebay.com/my/keys).
- `EBAY_USER_REFRESH_TOKEN` — from the Authorization Code grant with the
  `sell.inventory` scope.
- One-time setup of business policies and an inventory location, then fill
  `EBAY_FULFILLMENT_POLICY_ID`, `EBAY_PAYMENT_POLICY_ID`, `EBAY_RETURN_POLICY_ID`,
  `EBAY_MERCHANT_LOCATION_KEY`.

The listing flow (`app/services/ebay/sandbox.py`) follows the documented
[inventory item → offer → publish](https://developer.ebay.com/api-docs/sell/static/inventory/inventory-item-to-offer.html)
sequence. Sandbox and live differ only by host.

> Note: production eBay requires publicly reachable image URLs. In live mode you
> must host the card crops somewhere eBay can fetch them and populate `imageUrls`.

## Configuration (`.env`)

| Key | Meaning |
| --- | --- |
| `CONFIDENCE_THRESHOLD` | below this → `needs_review` (default 0.7) |
| `MIN_STORE_VALUE` | cards under this are stored but not listable (default $4) |
| `PRICE_MARKUP` | list price multiplier (default 1.5 = +50%) |
| `MAX_CARDS` | max cards detected per image (default 9) |
| `VERIFY_IDENTIFICATION` | run the second-pass verification (default true) |
| `MIN_EXACT_COMPS` | below this many exact comps → low-confidence note |
| `COMP_RECENCY_DAYS` | preferred comp recency window |

## Tests

```bash
source .venv/bin/activate
pytest
```

Covers safeguard gating, the ×1.5 / <$4 routing, vision JSON parsing (incl.
fenced/malformed), comp matching & exclusion, the stub listing payload, and an
end-to-end upload → repository → sell flow against in-memory SQLite.
