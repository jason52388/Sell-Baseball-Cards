# Sell Baseball Cards

Photograph baseball cards (up to 9 per image, mass-upload many images at once),
identify and grade each one with Claude vision, price them from eBay sold comps
(+ web-search fallback), keep the valuable ones in a reviewable repository, and
create eBay Buy-It-Now listings on demand, priced from what the card sells for.

## Quick start

```bash
cp .env.example .env          # add your ANTHROPIC_API_KEY
./run.sh                      # creates venv, installs deps, starts the server
```

Open http://127.0.0.1:8000 to upload, http://127.0.0.1:8000/review to check
cards one at a time, and http://127.0.0.1:8000/repository to browse and sell.
eBay runs in **preview mode** by default: no eBay account needed; the real
listing payload is built and shown to you but nothing is ever published.
Prices come from whichever comp sources you have configured; with none
configured a card simply reports no price rather than inventing one.

## Using the app

Every page has the same green header: **Upload**, **Review** (with the number
of cards that need a look), **Collection**, and a pill showing the eBay mode
(LIVE eBay, eBay sandbox or Preview only). A red banner appears under it when
a price source is down, for example an expired SportsCardsPro sign-in. The
look and wording rules are in [docs/design-notes.md](docs/design-notes.md).

1. **Upload** (`/`)
   1. Drop photos on the box (or click it to choose them). JPG, PNG and HEIC
      work, up to 9 cards per photo.
   2. Optionally type a batch tag, such as `1989 commons box`.
   3. Click **Identify cards**. Each photo gets a progress line: waiting,
      working (with the current step), done, or failed. A failed photo has
      **Retry** and **Split as grid** (cut the photo into even rows and
      columns). A photo you uploaded before is skipped, with **Add anyway**.
      Leaving and coming back picks the progress up again.
   4. Found cards wait under **Ready to add**. Use **Add**, **Edit**, or the
      `⋯` menu (Re-analyze, Pair with its other side, Unmatch back, Fix price
      with a SportsCardsPro link, Discard). A back with no front shows
      **Pick its front**: click it, then click the front. **Add all** and
      **Discard all** act on everything waiting (Discard all can be undone).
   5. **Options** holds the even-grid split, **Save to inbox for later**
      (no reading now; run `tools/ingest_folder.sh` later), and **Type a card
      in by hand**.
2. **Review** (`/review`): one card at a time, front and back side by side
   (click to zoom), why it needs a look, and every field with how sure the
   read was and which side it came from. Fix any field, then **Looks right**
   (Enter) saves the change, re-prices and moves on. **Re-analyze** (R) reads
   both sides again, **Skip** (right arrow) moves on, and the `⋯` button marks
   it as not a card or as a back. Shortcuts other than Enter are ignored
   while you type in a field.
3. **Collection** (`/repository`)
   - Five tiles: value from real sales, value from asking prices (amber,
     less reliable), cards that need review (click to start), live on eBay,
     sold this month.
   - Filter chips with counts: All, Needs review, Ready to list, Under $4,
     Listed, Sold, Duplicates, Unmatched backs, Deleted. Search, sport, tag,
     sort, and **More filters** (value range, possible PSA 10, unusual cards,
     no price). Filters and scroll position are kept while you move around.
   - **List** or **Grid** (remembered; phones always get the grid). Click a
     row to open the card; click a photo to zoom; the `⋯` menu has Open,
     Edit, Re-analyze, Unmatch back, Change price and End listing (when
     live), and Delete. Every delete shows **Undo**, and the **Deleted** chip
     can restore a card for 7 days.
   - Tick cards to get the selection bar: **List on eBay** (one listing each;
     the result shows why any card failed), **List as one lot**, **Refresh
     prices** (only the ticked cards). **Check for sales** asks eBay what
     sold; **Refresh all prices** re-prices every card in the background.
4. **Card page** (`/card/{id}`): big photos, the value and where it came
   from, the price it would list at, the eBay listing (List, Change price,
   End listing), details, every sale found, and how the card was identified.

## How it works

1. **Upload** (`/api/upload`): accepts multiple image files (JPEG, PNG, WebP,
   and iPhone HEIC, converted to JPEG first). The request only saves the photos
   and answers at once with a **job**; a background worker then processes the
   photos one at a time (see [Upload jobs](#upload-jobs)). Each image →
   Claude vision detects up to 9 cards (player, year, set, number, parallel,
   subset, team, rookie, condition) with a **per-field confidence** and the
   **raw text read** off the card. A photo with 2 or more cards is read in
   **two passes**: boxes first on a downscaled copy, then each card again from
   its own full-resolution crop, so small print survives. `parallel` holds
   only finish or numbering variants (Refractor, Gold /99); insert and subset
   names (League Leaders, Record Breaker) go in `subset`. One malformed card in
   the model's answer is skipped on its own instead of failing the photo.
   A **second-pass verification** re-checks each front (with its back, when
   one is already paired). A field it cannot see is unknown, not wrong; a
   correction it backs with printed evidence and high confidence is applied;
   any other disagreement lowers confidence so the card goes to review.
   When a **back pairs** to a front, the card's confidence is recomputed from
   both sides together (see the ingest skill, stage 4).
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
     "Add all" promotes every previewed card at once. It can take a while when
     the collection folder is on iCloud, so the button reads "Adding N…" until
     the server answers; then the added cards leave the review list and a line
     at the top confirms how many went to your library. If the add fails, the
     button comes back so you can try again.
   - **Re-analyze with the strongest model** (`POST /api/cards/{id}/reanalyze`):
     Claude when `VISION_PROVIDER=claude_cli` or a Claude API key is set, else
     `GEMINI_MODEL_HQ` (default `gemini-3.1-pro-preview`), falling back to
     `GEMINI_MODEL` when that model can't be used on your plan. It re-reads the
     front and, for a paired card, the back in the same request, keeps what
     the back supplied unless the new read is surer, and re-prices. A preview
     stays a preview; a library card is re-priced in place. A card listed on
     eBay is refused. Surfaced for low-confidence cards.
   - **Discard** (`DELETE /api/cards/{id}`): drop a previewed card. Like
     every delete it can be undone for 7 days (see
     [Deleting and restoring](#deleting-and-restoring)).
   - **Add / correct manually** via the manual form (`POST /api/cards/manual`).
5. **Safeguards** — low confidence, incomplete identity, or no comps →
   `needs_review` (never auto-priced or auto-listed). PSA 10 / anomaly cards are
   always kept and routed to `needs_review` for human valuation.
6. **Repository / library** (`/repository`) — every **added** card is its own
   library entry (un-added previews are excluded). Browse, filter (PSA 10 /
   anomalies), see your photo next to the **marketplace reference photo**, and
   open a **card detail** page showing the identification audit, a **last-sold
   price per marketplace** summary, and **every matching sold listing (with photo
   + clickable link)**. The **Duplicates** filter (`GET /api/cards/duplicates`)
   groups cards that look like the same physical card — either double-scanned or
   a genuine second copy. It only groups cards that agree on every identity field
   both of them carry, so two cards of the same player, year and set with
   *different* printed numbers are never grouped. A group is **certain** when
   player, year, set and number all match (with parallels agreeing), and
   **possible** when they agree on everything read but a number or parallel is
   missing on one — labelled with what was missing, so you decide rather than the
   app guessing.
7. **Sell** — for selected `priced` cards, creates eBay Buy-It-Now listings at
   the suggested list price (sold-priced: estimate x 1.15; asking-priced: median
   ask x 0.95; never below the fee floor; rounded up to the next 50 cents, see
   [docs/ebay-listing.md](docs/ebay-listing.md)). A card already live or sold on
   eBay is never listed twice. Tick cards in the collection, then use the
   selection bar: **List on eBay** lists them **individually**
   (`/api/listings/sell`, one listing each, with a result per card) and
   **List as one lot** uses `/api/listings/sell-set` (one combined **lot**
   listing). A set listing uses eBay's lot category (261329), bundles up to 24
   card photos (eBay's per-listing image cap), prices the lot at the **sum** of
   the cards' base prices (floor applied once), and builds an HTML description table covering
   **every** card (titles are capped at eBay's 80-char limit; if the lot has more
   than 24 cards the description notes which photos are shown).

## Upload jobs

A photo of nine cards takes minutes (detection, a close-up re-read of each
card, verification, pricing), so uploads run in the background:

1. `POST /api/upload` saves each original into `data/inbox/processed/` under a
   unique name, records its SHA-256, and returns the job right away.
2. One worker thread processes the photos **one at a time** (so the Claude CLI
   and the database are never asked to do two at once) and saves after every
   step. Each photo reports a step such as "Verifying card 2 of 6".
3. The page polls `GET /api/jobs/{job_id}`. After a page refresh,
   `GET /api/jobs/active` lists unfinished jobs (and, for a day, finished jobs
   that still have failed photos, until `POST /api/jobs/{job_id}/dismiss`).
4. A failed photo can be retried: `POST /api/jobs/{job_id}/retry/{photo_index}`.
   Add `?grid_rows=3&grid_cols=3` to split that one photo into an even grid
   on the retry (the upload page's **Split as grid**). If the server restarts
   mid-photo, that photo is marked failed with a retry note and the waiting
   photos carry on.

**Repeat uploads.** A photo already uploaded (same bytes) is skipped and
reported as "already uploaded (cards #12 Ken Griffey Jr., ...)". To add it
anyway, send `force=true` with the upload, or retry that photo with
`?force=true`.

**Refresh prices** (`POST /api/cards/reprice`) is a job too: one item per
library card, saved after each card. Send `{"card_ids": [...]}` to refresh
only those cards. Cards live or sold on eBay keep their
listed price and are skipped.

## Deleting and restoring

`DELETE /api/cards/{id}` moves a card to the trash instead of erasing it:

- It disappears from every list, count, pairing search and the review queue,
  and can be brought back for **7 days** with `POST /api/cards/{id}/restore`
  (it returns to the status it had). `GET /api/cards?status=deleted` lists the
  trash. Older deleted cards are removed for good, with their crop files, the
  next time the app starts.
- A card **listed on eBay** has its listing ended first. If eBay refuses, the
  card is not deleted and the answer is a 502 with eBay's message.
- A card that **sold** on eBay needs `?confirm=true`, since its record is the
  sale's history.

Merging a card into another as its back (mark as back, attach back, pair)
removes that card, so the same care applies: a card live or sold on eBay is
refused (409), a card already in the collection needs `?confirm=true`, and a
card with its own back attached must be unmatched first. Attaching a new back
to a card that already has one keeps the old back as an unmatched back instead
of deleting its image.

## Source photos are archived after the last card is added

With `COLLECTION_PHOTOS_DIR` set, a source photo moves from
`data/inbox/processed/` to the collection folder only once **none** of its
cards is still waiting in the upload queue (each card added or discarded, and
at least one added). It is named neutrally, batch tag or upload date plus the
original name (`box 7 IMG_0042.jpg`), because one photo can hold nine cards.
Crops are still copied per card under the card's own name.

## Review queue

`GET /api/review/next?after_id=` returns the next library card marked
`needs_review` (in id order, wrapping round), with the reason it is there, the
front and back crop URLs, how many remain, and for every identity field its
value, confidence and the side it was read from (`front`, `back`, `both`,
`verifier`, or `user` for a value you typed). `POST /api/cards/{id}/confirm`
accepts the identity as it stands: confidence 1.0, recorded as confirmed, and
the card is re-routed (priced, below threshold, or still in review only for a
reason confirming cannot settle, such as a PSA 10 candidate or no price yet).
Editing a field (`PATCH /api/cards/{id}`) re-prices the card and keeps it in
the library.

## Collection numbers

`GET /api/cards/stats` reports the collection value split by where it came
from (`value_from_sold`, `value_from_asking`, with counts), the list value at
the same suggested list price a listing would use, and counts for review,
ready to list, below the store threshold, under the listing floor, live on
eBay (with value), sold (total and this month), duplicates, unmatched backs,
queued and deleted cards. Every card also carries `listing_state` (none, live,
ended, sold), `suggested_list_price` and `price_floor`.

## Bulk identify with your Claude subscription (no API key)

**Simplest: make Claude the app's reader.** Set these in `.env` and restart the
app. Every in-app upload, Re-analyze and verification then runs through headless
Claude Code on your subscription, with no API key:

```bash
VISION_PROVIDER=claude_cli
CLAUDE_CLI_MODEL=claude-opus-5-5
```

`claude` must be on the server's PATH and logged in. Each photo takes roughly 20
seconds. The folder script below is the batch alternative.

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

The server keeps its own copy of every ingested photo in `data/inbox/processed/`
(so it archives later, wherever the folder was). After each photo the script:

| Outcome | What happens to the photo |
|---|---|
| Ingested | Website inbox: removed (the server has its copy). Any other folder: moved to `FOLDER/processed` |
| Already uploaded (409) | Moved to `FOLDER/duplicates` |
| No cards found (422) | Moved to `FOLDER/failed`, with a `.txt` note saying why |
| Any other failure | Left in place, so the next run tries it again |

Moves never overwrite: a name already taken gets a `-2`, `-3` suffix. HEIC photos
are converted with macOS `sips` first, and files that are not photos are listed
as skipped.

> `/api/ingest` accepts `multipart/form-data` with an `image` file and a
> `detections` field (`{"cards":[...]}` or a bare list). Anything that produces
> that schema can feed it; Claude Code is just the included driver. Ingested
> fronts are verified like uploads (when the app has a vision provider); send
> `verify=false` to skip that for a request.

## Pricing accuracy

**No fabricated data, ever.** Prices come only from real eBay data. If none is
available the card is flagged `needs_review` — a price is never invented.

The app shows **two real prices side by side** for every card (`sold_estimate`
and `active_estimate`), pulling from any combination of these real sources:

| Kind | Source | Credentials / cost |
| --- | --- | --- |
| **Current asking** | eBay **Browse API** | Free app keyset — works immediately |
| **Last sold** | eBay **Marketplace Insights API** | Free keyset **+ eBay approval** of `buy.marketplace.insights` |
| **Last sold** | **PriceCharting / SportsCardsPro API** | Paid token (`PRICECHARTING_TOKEN`) — works immediately. Aggregate price + full grade-tier breakdown |
| **Last sold** | **SportsCardsPro individual sales** | `SPORTSCARDSPRO_SALES_ENABLED=true` + token. Scrapes the product page's recent-sales list. Best-effort, ToS-gray |
| **Last sold** | **130point** | `POINT130_ENABLED=true`. Free; captures the **best-offer-accepted** prices eBay hides, plus PWCC/Goldin/etc. Best-effort, ToS-gray |
| **Last sold** | **Headless-browser eBay scrape** | Free; `EBAY_BROWSER_SCRAPE_ENABLED=true` + Playwright. Best-effort, ToS-gray |

The estimate prefers real **sold** data and falls back to **active asking**
prices, always labeling which basis it used (`price_basis`). **All sold sources
are pooled** for the "Last sold" estimate (median of the outlier-trimmed set).
The same sale reported by two sources (say eBay Insights and 130point) counts
once; `PRIMARY_SOLD_SOURCE` (default `sportscardspro`) only decides which copy is
kept. The derivation says exactly what the price rests on: recent sold sales,
SportsCardsPro **market averages** (one number per grade, not a sale), or undated
sales. When fewer than `MIN_EXACT_COMPS` (3) individual sales back the price, the
derivation says so and the card is flagged low-confidence. Undated sales never
count as recent; they price a card only when nothing dated exists, and are
labelled. Asking prices include the cheapest listed shipping, so they compare
with sold prices (what the buyer paid). All configured
sources are still merged and shown on the card-detail page, each tagged with its
**provider** (who the data came through — 130point / SportsCardsPro / eBay) and
the **original marketplace** the sale happened on (eBay / PWCC / Goldin / …), so
you can see exactly where every price came from. The card-detail **"All sales"**
section lists every individual completed sale, grouped by provider.

> **Price history accumulates.** When a card's cached comps are refreshed, real
> dated sales are *merged* into the stored set rather than overwritten, so sale
> history builds up even after sales age out of a source's lookback window.
> Active asking prices and market averages are always replaced (keeping stale
> copies would be wrong). Retention is `PRICE_HISTORY_RETENTION_DAYS` (default
> 365). A cached result of sold comps is reused for `PRICE_CACHE_TTL_DAYS`
> (default 30); one that also holds asking prices or a SportsCardsPro average is
> reused for `PRICE_CACHE_ACTIVE_TTL_DAYS` (default 7). Empty results, and
> results where any source failed, are never cached. The "Refresh prices" button
> fetches fresh data at any time.

### Which comps count

A comp is **excluded** (kept visible, with the reason) when it is not a sale of
this exact card:

| Excluded | Examples |
| --- | --- |
| Junk listings | lots ("lot of 5", "x10", "(10)", bundle, you pick), reprints ("RP"), customs, ACEO, art cards, digital/NFT/Topps Bunt, breaks, facsimile, replica |
| A different player | the player's full name must be in the title |
| A parallel the card does not have | Gold, Refractor, Prizm, Holo, Foil, Chrome (unless the set is Chrome), Xfractor, Atomic, Sapphire, Black, "parallel", serial numbers ("/50", "1/1"), printing plates, SP/SSP, variations. Whole word only ("Goldschmidt" is fine), and a word that is part of the card's own set or parallel is allowed ("Topps Gold Label") |

Graded sales are recognised even with grade words in between ("PSA Gem Mint
10", "BGS Pristine 10", "SGC 9.5"), and when eBay reports the condition simply as
"Graded". Autograph authentication alone ("PSA/DNA") is not a grade. The same
rules pick the SportsCardsPro product, which must also carry the player's last
name, for both the price and the reference photo.

### Price source health

Every fetch records a status per source: `ok`, `empty`, or a failure
(`error`, `auth_expired`, `unauthorized`, `blocked`, `quota`). A failing source
never fails the card. Instead:

- the card's review reason **leads** with it (for example "Price source problem:
  SportsCardsPro rejected the API token ...") rather than telling you to
  re-check a correct identification;
- `GET /api/sources/health` returns the last state, last error and last success
  time per source, plus a `banner` string for the UI.

```json
{
  "sources": [{"source": "sportscardspro", "label": "SportsCardsPro",
               "state": "auth_expired", "ok": false, "message": "...", "count": 0,
               "last_checked_at": "...", "last_success_at": null,
               "last_error": "...", "last_error_at": "..."}],
  "problems": ["(the entries above with ok = false)"],
  "banner": "SportsCardsPro: ... | null"
}
```

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

### Extra sold-data sources (130point & SportsCardsPro sales)

Two extra sold-data sources widen the comp pool with **individual** sales:

- **130point** (`POINT130_ENABLED=true`) — surfaces the real **best-offer-accepted**
  prices eBay hides on public completed listings, and pools sales from eBay, PWCC,
  Goldin and others. Free, no token.
- **SportsCardsPro individual sales** (`SPORTSCARDSPRO_SALES_ENABLED=true`, needs
  `PRICECHARTING_TOKEN`) — the API only returns *aggregate* prices, so this scrapes
  the product page's recent-sales table for the actual dated sales.

Both are **scrapers, not official APIs** (ToS-gray), so they ship **off by
default** and degrade to nothing — never a fake price — if a site blocks the
request or changes its markup. A block (Cloudflare challenge, HTTP 403/429/503,
or 130point's empty stub response) is reported as source status `blocked`, so
it shows up in `/api/sources/health` instead of looking like "no sales".
**Verify them against the live sites before
relying on them** (the parsers depend on page structure that can drift):

```bash
python3 -m tools.verify_130point "1989 Upper Deck Ken Griffey Jr #1"
PRICECHARTING_TOKEN=… python3 -m tools.verify_sportscardspro "1989 Upper Deck Ken Griffey Jr #1"
```

Each tool prints what it parsed (and accepts `--raw` to dump the page HTML). If a
tool parses 0 sales but the site shows them, the selectors need updating in
`app/services/point130.py` / `app/services/pricecharting.py` — see `tools/README.md`.

`app/services/comp_sources.py` is the single place that fans out across sources
(`insights.py` = sold, `browse.py` = active, `pricecharting.py` = SportsCardsPro,
`point130.py` = 130point). The rest of the app (matching, recency,
outlier-trimming, UI, listing) is source-agnostic.

## Going live with eBay

Set `EBAY_MODE=sandbox` (then `live`) in `.env` and fill in:

- `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` — from your
  [eBay developer app keyset](https://developer.ebay.com/my/keys).
- `EBAY_USER_REFRESH_TOKEN`: visit `/ebay/oauth/start`; it asks for the
  `sell.inventory`, `sell.account` and `sell.fulfillment` scopes and writes the
  token into `.env` (no restart needed). A token made before `sell.fulfillment`
  was added still lists fine, but the sold sync needs one re-authorization.
- One-time setup of business policies and an inventory location, then fill
  `EBAY_FULFILLMENT_POLICY_ID`, `EBAY_PAYMENT_POLICY_ID`, `EBAY_RETURN_POLICY_ID`,
  `EBAY_MERCHANT_LOCATION_KEY`.
- Every listing sends a package weight and size (a 7 x 4 x 1 inch padded
  envelope, `EBAY_PACKAGE_WEIGHT_OZ`, default 3 oz, plus
  `EBAY_LOT_EXTRA_CARD_WEIGHT_OZ` per extra card in a lot). A calculated
  shipping policy cannot publish without it.

The listing flow (`app/services/ebay/sandbox.py`) follows the documented
[inventory item → offer → publish](https://developer.ebay.com/api-docs/sell/static/inventory/inventory-item-to-offer.html)
sequence. Sandbox and live differ only by host.

Photos (front and back) are uploaded to eBay Picture Services, so listings do
not depend on the tunnel; `PUBLIC_IMAGE_BASE_URL` is only a checked fallback.
After listing you can change the price, end the listing, and sync sales from eBay
orders. The full listing rules (condition, title, item specifics, price, Best
Offer) and every endpoint are in [docs/ebay-listing.md](docs/ebay-listing.md).

## Configuration (`.env`)

| Key | Meaning |
| --- | --- |
| `CONFIDENCE_THRESHOLD` | below this → `needs_review` (default 0.7) |
| `MIN_STORE_VALUE` | cards under this are stored but not listable (default $4) |
| `PRICE_MARKUP` | list price multiplier for cards priced from SOLD comps (default 1.15) |
| `EBAY_ASK_UNDERCUT` | list price multiplier for cards priced from ASKING prices (default 0.95) |
| `EBAY_SHIPPING_SUPPLIES_COST` / `EBAY_MIN_NET` | feed the list-price floor with the eBay fees (defaults 1.00 / 0.50) |
| `EBAY_BEST_OFFER_AUTO_ACCEPT_PCT` | Best Offer auto-accept as a fraction of list (default 0.80) |
| `EBAY_INCLUDE_REFERENCE_IMAGE` | also send the other seller's reference photo (default false) |
| `EBAY_UPLOAD_IMAGES` | upload listing photos to eBay Picture Services (default true) |
| `MAX_CARDS` | max cards detected per image (default 9) |
| `VERIFY_IDENTIFICATION` | run the second-pass verification (default true) |
| `VERIFY_CORRECTION_MIN_CONFIDENCE` | a verifier correction is applied only at or above this, with a reason (default 0.85) |
| `TWO_PASS_DETECTION` | re-read each card from its own crop when a photo has 2+ cards (default true) |
| `DETECTION_PASS1_MAX_EDGE` | long edge of the copy used to find boxes in pass 1 (default 2000 px) |
| `TWO_PASS_CONCURRENCY` | pass-2 crop reads run at once (default 3) |
| `VISION_MAX_EDGE` / `VISION_MAX_BYTES` | images sent to a provider are downscaled to fit (default 3000 px / 3.75 MB) |
| `MIN_EXACT_COMPS` | below this many exact comps → low-confidence note |
| `COMP_RECENCY_DAYS` | preferred comp recency window |
| `CROP_PADDING_PCT` | margin kept around each detected card (default 0.08) |
| `SINGLE_CARD_PAD_PCT` | wider margin used when a photo holds one card (default 0.25) |
| `CROP_AUTOSTRAIGHTEN` | deskew a tilted card after cropping (default false) |

## Tests

```bash
source .venv/bin/activate
pytest
```

Covers safeguard gating, the <$4 routing, the list-price rule, vision JSON parsing (incl.
fenced/malformed), comp matching & exclusion, the eBay listing payload (including
the Card Condition descriptors a live publish requires), the account-deletion
challenge hash, and an end-to-end upload → repository → sell flow against
in-memory SQLite.

The suite is **hermetic**: an autouse fixture in `tests/conftest.py` redirects
crops, the inbox and reference images to a temp directory and disables photo
archiving, so running `pytest` never touches `data/` or your
`COLLECTION_PHOTOS_DIR`. Keep it that way — a test that writes to a real data
directory will quietly fill your collection folder with fixtures. The same suite
runs on every push via `.github/workflows/tests.yml`.
