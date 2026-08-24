---
name: ingest
description: >
  The full card-photo ingest pipeline for this project — from image upload through
  detection, cropping, AI identification, front/back pairing, pricing, preview,
  promotion to the collection, and photo archival. Use this skill whenever you need
  to understand, debug, modify, or extend any part of the ingest flow. Also use it
  when the user mentions uploading photos, card detection, cropping, pairing fronts
  and backs, pricing, promoting/adding cards, archiving photos, or any of the
  services in app/services/ (vision, cropping, pairing, pricing, photo_archive).
  Trigger proactively when working on upload.py, cards.py promote endpoint,
  or any ingest-related service file.
---

# Card Photo Ingest Pipeline

## Pipeline overview

Photos enter the system, individual cards are detected by AI vision, cropped,
identified, optionally paired front-to-back, priced from real sold comps, shown
to the user as a preview, and finally promoted into the collection with source
photos archived.

```
Photo upload ─► Detection (Claude/Gemini) ─► Filter phantoms ─► Crop each card
  ─► Pair front/back ─► Verify identity ─► Price from comps ─► Preview
  ─► User promotes ─► Finalize + archive photos ─► Collection
```

## Stage 1: Upload entry points

Three routes accept images (`app/routers/upload.py`):

| Route | Function | Purpose |
|-------|----------|---------|
| `POST /api/upload` | `upload()` | Interactive: detect + crop + price in one request |
| `POST /api/queue` | `queue_photos()` | Drag-drop to inbox for async processing later |
| `POST /api/ingest` | `ingest()` | External: pre-identified cards with JSON detections |

All uploads create an `ImageUpload` row (`app/models.py`) tracking the original
filename and card count. An optional `batch_tag` form param groups cards.

File locations:
- Originals land in `data/inbox/` (queued) or `data/inbox/processed/` (ingested)
- Crops go to `data/crops/`

## Stage 2: Detection

`app/services/vision.py` — `detect_cards()`

The vision model reads the photo and returns up to `MAX_CARDS` (default 9)
`DetectedCard` objects, each with: player, year, sport, side (front/back),
set_brand, card_number, parallel, serial_number, condition, confidence (0-1),
bbox [x,y,w,h] normalized 0-1, field_reads (per-field confidence), raw_text,
and grading/anomaly flags.

**Provider selection** (`_provider()`):
- Anthropic Claude (preferred) — `ANTHROPIC_API_KEY` + `ANTHROPIC_MODEL` (default `claude-opus-4-8`)
- Google Gemini (fallback) — `GEMINI_API_KEY` + `GEMINI_MODEL` (default `gemini-2.5-flash`)
- Auto mode: use Claude if key present, else Gemini

**Grid mode**: When the user specifies `grid=(rows, cols)`, the image is split
into equal cells (`cropping.grid_cells()`), each cell identified independently
via `_detect_by_grid()` — useful for sheets/pages with a regular grid layout.

**Phantom filter** (`upload.py` — `_is_phantom_detection()`): Rejects detections
with tiny bboxes (< 3% of image area) or low confidence + no player + no card
number.

Both rules are skipped for grid detections (`from_grid=True`). Grid cell boxes
come from an even split, so their size carries no signal — a 10x10 cell is 1% of
the photo and every card in a dense grid would be discarded — and an unreadable
cell is meant to survive as a low-confidence preview the user can fix.

## Stage 3: Cropping

`app/services/cropping.py` — `crop_card()`

For each detected card:
1. Apply EXIF rotation so the image is upright
2. Convert to RGB
3. Denormalize bbox to pixel coordinates, clamp to image bounds
4. Extract tight rectangle
5. Save as JPEG quality 90 to `data/crops/{card_id}-{uuid}.jpg`

## Stage 4: Front/back pairing

`app/services/pairing.py` — `try_pair()`

Runs automatically after each detection. If the new card is a back, it searches
for a matching front (and vice versa).

**Matching priority** (`_unique_match()`):
1. **Strong key**: year + card_number (both sides print these)
2. **Weak key**: year + normalized player name
3. **EXIF timestamp**: photos taken < 10 seconds apart (`_closest_by_timestamp()`)

When paired:
- `remember_pre_pair_identity()` snapshots the front's own identity first, so
  unmatching a wrong back can undo what it overwrote
- `enrich_front_from_back()` backfills missing fields on the front
- `remember_back_source()` stores the back's original filename for archival
- Back row is deleted; its crop path moves to `card.back_crop_path`
- Front is re-priced with the enriched identity via
  `pricing.reprice_after_pairing()`

**Pairing never moves a card backwards.** Photographing fronts first and backs
later is the normal workflow, so a back routinely pairs to a card that is
already in the collection. `reprice_after_pairing()` keeps a preview in preview,
re-prices and re-routes a library card *without* returning it to preview, and
leaves a card with a live eBay listing untouched (its price is the listed one).
Using `preview_card()` here instead would silently drop promoted cards out of
the collection.

**Manual pairing endpoints** (`app/routers/cards.py`):
- `POST /api/cards/{front_id}/attach-back/{back_id}`
- `POST /api/cards/{a_id}/pair/{b_id}`
- `POST /api/cards/{front_id}/detach-back` (unmatch)

## Stage 5: Verification

`app/services/vision.py` — `verify_card()`

When `VERIFY_IDENTIFICATION=true` (default), a second vision pass runs on the
crop alone with the proposed identity. If the verifier disagrees, confidence is
lowered to 0.4 so safeguards flag it for review.

**Re-analysis** (`POST /api/cards/{card_id}/reanalyze`): User-triggered
re-identification using the strongest available model (Claude if key set, else
`gemini-2.5-pro`).

## Stage 6: Pricing

`app/services/pricing.py` — `preview_card()` / `price_card()`

**Safeguard gate** (`_gate()`): Skips pricing if confidence < `CONFIDENCE_THRESHOLD`
(0.7) or missing core identity (player AND year-or-set). Failed cards get
`STATUS_NEEDS_REVIEW`.

**Comp gathering** (`app/services/comp_sources.py` — `gather_comps()`):

| Source | Type | Config |
|--------|------|--------|
| eBay Marketplace Insights | Sold | `EBAY_INSIGHTS_ENABLED` |
| SportsCardsPro / PriceCharting | Sold | `PRICECHARTING_TOKEN` |
| 130point.com | Sold (incl. best-offer) | `POINT130_ENABLED` |
| eBay headless scrape | Sold | `EBAY_BROWSER_SCRAPE_ENABLED` |
| eBay Browse API | Active asking | eBay keyset (free) |
| Web search | Fallback | `WEBSEARCH_API_KEY` |

Comps are cached persistently per (query, graded, marketplace) with a TTL of
`PRICE_CACHE_TTL_DAYS` (default ~100 years, effectively permanent).

**Scoring** (`app/services/matching.py` — `score_comp()`): Each comp is scored
as exact, near, graded, or excluded. This one file decides which prices count,
so its rules are deliberately strict:

- **exact** = player + any 2 of year/set/number (any two — a card with no year
  still prices off set + number)
- All token matching is **whole-word**: substring matching let "Bo" match inside
  "Bob" and the year "1989" match inside "219890"
- **Set** requires every significant word ("Topps Chrome" is not "Topps")
- **Parallel**, when the card has one, must appear in the title or the comp
  cannot be exact — a base sale is worth a fraction of its /50 parallel
- **graded** covers PSA/BGS/SGC/CSG/**CGC**. Keep this list in sync with
  `pricecharting._GRADE_RE`: a grader missing here is counted as a raw sale, and
  slab prices are many times the raw price.

**Graded estimate**: only comps that scored `graded` feed
`graded_value_estimate`. Sources answer the graded query with raw sales mixed
in, so counting everything understated the PSA 10 upside badly.

**Estimate**: Median of outlier-trimmed exact comps. Prefers SOLD over ACTIVE.
Primary sold source is `PRIMARY_SOLD_SOURCE` (default `sportscardspro`).

**Reference image**: Best marketplace photo is downloaded locally to
`data/ref_images/`.

**Status routing** (`_route_status()`):
- No price → `needs_review`
- PSA10 candidate or anomaly → `needs_review`
- Price < `MIN_STORE_VALUE` ($4) → `below_threshold`
- Otherwise → `priced`

During preview, status is always set to `preview` regardless of routing.

## Stage 7: Preview

Cards land in `STATUS_PREVIEW`. They appear on the upload page but not in the
main collection view. The user sees the crop, estimated price, comps, reference
photo, and confidence badge.

**User actions on preview cards:**
- Add to repository (promote)
- Discard (delete)
- Re-analyze (stronger model)
- Edit identity fields manually
- Pair front/back manually

## Stage 8: Promotion

`app/routers/cards.py` — `POST /api/cards/promote` — `promote_cards()`

For each card being promoted:
1. `finalize_card()` re-runs safeguards and routes status (priced / needs_review /
   below_threshold)
2. Source photo filenames are queued for archival (front + back)
3. Crop file paths are queued for collection copy (front + back)

After DB commit:
- `photo_archive.archive_source_files()` moves originals out of inbox (best-effort)
- `photo_archive.archive_crop_files()` copies crops to the collection folder

Card is now in the library, visible in the collection view.

**Duplicate detection** (`app/services/dedupe.py` — `find_duplicates()`): the
collection's **Duplicates** filter (`GET /api/cards/duplicates`) groups library
cards that look like the same physical card. It compares only identity fields
both cards carry, so any disagreement rules the pair out. `certain` = player,
year, set and number all present and equal with parallels agreeing; `possible` =
agrees on everything read but a number or parallel is missing on one. Ambiguity
is never guessed: a card with no number joins a numbered card only when exactly
one number is in play. Backs and previews are excluded.

## Stage 9: Photo archival

`app/services/photo_archive.py`

Controlled by `COLLECTION_PHOTOS_DIR` (blank = disabled). When set:

**Source photos** are MOVED from `data/inbox/processed/` to the collection folder
— cleans up the working inbox.

**Crop images** (front + back) are COPIED to the collection folder — the app
still needs the originals in `data/crops/`.

Files are renamed to match the card description:
`{Player}, {Manufacturer}, {Year}, {Parallel} (front).jpg`

Example: `Mike Trout, Topps Chrome, 2023, Refractor (front).jpg`

Duplicate filenames get a numeric suffix. Failures are logged but never block
card addition.

## Key data model

`app/models.py`

**ImageUpload**: original filename, card_count, batch_tag, created_at

**Card**: Full card record with identity fields (player, year, sport, set_brand,
card_number, parallel, serial_number, condition, confidence), crop paths
(crop_path, back_crop_path), pricing fields (estimated_price, sold_estimate,
active_estimate, price_basis, derivation, etc.), grading fields
(grade_estimate, gem_mint_score, psa10_candidate), anomaly flags, workflow
status, and relationships to comps/listings.

**Comp**: Individual comparable sale tied to a card — title, price, date, source,
graded flag, matching score.

## Key config settings

All in `app/config.py` (set via `.env` file). The app caches settings at startup
via `@lru_cache` — **restart required** after `.env` changes.

| Setting | Default | Purpose |
|---------|---------|---------|
| `VISION_PROVIDER` | auto | auto / anthropic / gemini |
| `ANTHROPIC_MODEL` | claude-opus-4-8 | Detection model |
| `CONFIDENCE_THRESHOLD` | 0.7 | Below this → needs_review |
| `VERIFY_IDENTIFICATION` | true | Second-pass verification |
| `MIN_STORE_VALUE` | 4.0 | Below this → below_threshold |
| `MIN_EXACT_COMPS` | 3 | Low-comp warning threshold |
| `PRICE_CACHE_TTL_DAYS` | 36525 | Comp cache lifetime |
| `COMP_RECENCY_DAYS` | 90 | Only comps within this window |
| `PRIMARY_SOLD_SOURCE` | sportscardspro | Preferred comp source |
| `COLLECTION_PHOTOS_DIR` | (blank) | Archive folder; blank = disabled |
| `EBAY_MODE` | preview | preview / sandbox / live |
