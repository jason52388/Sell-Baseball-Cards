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

**The model must see the photo upright.** Phone photos store their pixels
sideways plus an EXIF flag saying how to turn them. The vision APIs read the raw
pixels and ignore the flag, while cropping applies it, so a box read off the
sideways pixels lands on the wrong part of the upright photo and the crop slices
through the card. `_generate()` therefore passes every image through `_upright()`
first (detect, re-analyze and verify all go through it). The folder ingest
(`tools/ingest_folder.sh`) does the same by showing Claude Code an upright copy
from `tools/upright_copy.py` while uploading the original. Boxes stored before
that fix were in sideways coordinates; `tools/recrop_rotated.py` re-cuts those.

The vision model reads the photo and returns up to `MAX_CARDS` (default 9)
`DetectedCard` objects, each with: player, year, sport, side (front/back),
set_brand, card_number, parallel, subset, team, rookie, serial_number,
condition, confidence (0-1), bbox [x,y,w,h] normalized 0-1, field_reads
(per-field confidence), raw_text, and grading/anomaly flags.

**parallel vs subset.** `parallel` is only a finish or numbering variant
(Refractor, Gold /99, Holo). An insert or subset banner (League Leaders,
Record Breaker, Magic Moments, All-Star, Highlights) goes in `subset`. The
prompt's PRICE DRIVERS section also teaches the exact product (Topps vs Topps
Chrome, Bowman Chrome, Finest, Stadium Club, Upper Deck SP), refractor and foil
finishes, stamped serials, the RC logo (`rookie`), vintage copyright year vs
season, and `team`. `tools/split_subset_from_parallel.py` moves subset names
that older reads stored in `parallel`.

**Tolerant parsing** (`schemas.py`, `vision.parse_detection`). Numbers are
coerced to text (`"year": 1989` becomes "1989"), a null confidence reads as
0.3 (low, so the card goes to review), a null or malformed bbox becomes `[]`
(the phantom filter then drops that card), and each card is validated on its
own: a card the schema still rejects is logged and skipped, never allowed to
fail the rest of the photo. Code fences are stripped anywhere, and a JSON answer
wrapped in prose is recovered (`_first_json_object`); verification replies get
the same salvage.

**Two-pass detection** (`TWO_PASS_DETECTION`, default on). A whole photo of 9
cards loses the small print. Pass 1 reads a copy downscaled to
`DETECTION_PASS1_MAX_EDGE` (2000 px) for the boxes. When it finds 2 or more
cards, pass 2 (`vision._reread_each`) re-reads each card from its padded
full-resolution crop (`cropping.padded_crop_bytes`, `CROP_USER` prompt),
`TWO_PASS_CONCURRENCY` at a time. The identity comes from the close-up read;
the box stays pass 1's, so the saved crop is cut in the same place. A crop that
fails or reads neither a player nor a number keeps its pass-1 card. A
single-card photo keeps its one read.

**Size guard.** `_generate` downscales every image (never upscales, never
crops) to `VISION_MAX_EDGE` (3000 px) and `VISION_MAX_BYTES` (3.75 MB) before
any provider sees it (`_fit_for_provider`). Boxes are normalized, so they are
unaffected.

**Several images per request.** `_generate` accepts a list (a card's front
then its back) on every provider; the Claude CLI is told to Read each temp
file in order. Re-analysis and verification use this.

**Crop re-reads** (`vision.reidentify`, used by grid cells, pass 2 and
re-analysis) take the detection with the largest, most central box
(`pick_central_card`), not the first one, since a crop often catches slivers
of neighbouring cards. A card with no box is taken to cover the whole crop.

**Provider selection** (`_provider()`):
- Claude Code CLI (`VISION_PROVIDER=claude_cli`) — runs `claude -p` on the
  user's Claude subscription, no API key. The upright photo is written to
  `data/.vision_tmp/`, Read by the CLI, then deleted. `CLAUDE_CLI_MODEL`
  (default `claude-opus-5-5`), `CLAUDE_CLI_TIMEOUT` (default 300 s). Roughly
  20 seconds per photo. Never chosen by auto mode: it must be set explicitly.
- Anthropic Claude API — `ANTHROPIC_API_KEY` + `ANTHROPIC_MODEL` (default `claude-opus-5-5`)
- Google Gemini — `GEMINI_API_KEY` + `GEMINI_MODEL` (default `gemini-2.5-flash`)
- Auto mode: use the Claude API if its key is present, else Gemini

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
4. **Pad** the box outward (see below) and extract that rectangle
5. Optionally straighten it (off by default)
6. Save as JPEG quality 90 to `data/crops/{card_id}-{uuid}.jpg`

**A crop must never lose part of the card.** Two rules enforce that, because a
clipped border, name plate, or card number breaks identification and pricing
downstream, while a sliver of extra background costs nothing.

**Padding.** The vision model's boxes routinely sit a few pixels inside the card,
so every box grows by `CROP_PADDING_PCT` (0.08) of its own size per side before
cropping. When a photo yields exactly **one** real detection (phantoms don't
count, grid splits are excluded), `upload._cards_from_detections()` uses
`SINGLE_CARD_PAD_PCT` (0.25) instead: with no neighbouring card to crowd, the
margin is free. Set it very high (e.g. 5.0) to keep the whole photo as the crop.
Padding only ever grows the box (`cropping._padded_rect` treats a negative pad
as zero); two-pass detection's in-memory crops use the same rule.

**Straightening may only enlarge.** `_refine_and_deskew()` finds the card's quad
with OpenCV and perspective-warps it upright. It is gated on
`CROP_AUTOSTRAIGHTEN`, **off by default**: straightening can only ever shrink the
view, so a misfire crops into the card. When enabled, `_covers_card()` rejects
any quad that fails to span `_MIN_KEEP_COVERAGE` (88%) of the detected card box
on both axes. That is what stops the classic failure: a card's inner art window
is card-shaped and dominates the frame, so the old area-and-aspect gate accepted
it and the crop zoomed into the artwork, cutting off the border and card number.
An art window is narrower and much shorter than the card (a name plate eats the
bottom), so the coverage test rejects it while the card's real outline, or the
toploader around it, passes.

## Stage 4: Front/back pairing

`app/services/pairing.py` — `try_pair()`

Runs automatically after each detection. If the new card is a back, it searches
for a matching front (and vice versa).

**Matching priority** (`_unique_match()`):
1. **Strong key**: year + card_number (both sides print these)
2. **Weak key**: year + normalized player name
3. **EXIF timestamp**: photos taken < 10 seconds apart (`_closest_by_timestamp()`)

**A different player alone is not a mismatch.** League-leader and combo cards
print one player on the front and another on the back (2000 Topps Griffey with
McGwire, 2001 Topps Pedro Martinez with Randy Johnson), so those must pair. Two
signals rule a candidate out instead:

- `_contradicts()`: the players differ AND the years are more than one apart
  (one apart is normal, backs print the prior year's copyright). Applies to
  every key.
- `_claimed_elsewhere()`: the timestamp fallback skips a candidate whose own
  identity (strong or weak key) matches another card on this side. `try_pair()`
  passes those as `rivals`: fronts with no back, or other orphan backs.

Without them, a Pete Rose back (1989 #505) whose own match was ambiguous (two
copies of the Rose front) fell to the Roy Halladay front shot seconds later,
which took Rose's #505 and was priced as the wrong card. A wrong back is worse
than none: it overwrites the front's number and price.

When paired:
- `remember_pre_pair_identity()` snapshots the front's own identity (and its
  confidence) first, so unmatching a wrong back can undo what it overwrote
- `enrich_front_from_back()` backfills missing fields on the front (year,
  number, set, parallel, sport, team, subset) and then recomputes the
  confidence (below)
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

**Confidence of the combined identity** (`pairing.combined_confidence`). The
detection confidence is the front's read alone, and fronts rarely print the
year or number, so honest front scores sit around 0.55-0.68, under the 0.7
gate, even when the back read both clearly. On pairing (automatic or manual
attach, both go through `enrich_front_from_back`):

1. Per core field (player, year, set_brand, card_number), take the best
   confidence among the sides whose reading equals the value the card now
   carries.
2. Combine as a weighted mean: player 0.35, card_number 0.25, year 0.20,
   set_brand 0.20.
3. Cap at the stronger side's own overall confidence (the front's pre-pair
   score, the back's detection score; older audits without one use the mean
   of their core field reads).
4. Only raise: a result at or below the current confidence changes nothing.

Nothing is raised when the sides contradict (different players, or both read
a year or number and they differ) or the verifier already disagreed with the
front. Each side's own overall score is stored as `confidence` in its
identification audit. Unmatch restores the snapshot's confidence.
`tools/recompute_paired_confidence.py` applies the rule to cards paired
earlier (dry run unless `--apply`; `--reprice` re-prices the raised cards).

**Manual pairing endpoints** (`app/routers/cards.py`):
- `POST /api/cards/{front_id}/attach-back/{back_id}`
- `POST /api/cards/{a_id}/pair/{b_id}`
- `POST /api/cards/{front_id}/detach-back` (unmatch)

Unmatch restores the front's identity from its pre-pair snapshot. A front paired
before snapshots existed has none, so `restore_pre_pair_identity()` falls back to
the front's own photo reading: a field that still equals what the back read, and
differs from what the front read, was lent by the back and is reverted. A value
the user has since typed no longer equals the back's, so it is kept.

## Stage 5: Verification

`app/services/vision.py` — `verify_card()`

When `VERIFY_IDENTIFICATION=true` (default), a second vision pass checks each
front (`upload._verify_front`). It runs for uploads and for `/api/ingest`
(the folder ingest) alike; ingest takes a `verify` form field, and
`verify=false` skips it for that request.

Order for a new front: it first absorbs a matching back uploaded earlier
(`try_pair`), then the verifier checks the combined identity against the front
crop plus the back crop when one is paired, then the card is priced once.

- `agree=true`: nothing changes. `agree=null`: the verifier could not confirm
  (a front often shows no year or number); that is unknown, not disagreement,
  and nothing changes. The prompt lists such fields in `unverifiable`.
- A correction (`{"value", "confidence", "reason"}`) is applied only when it
  names its evidence and is at least `VERIFY_CORRECTION_MIN_CONFIDENCE` (0.85)
  sure. Any other correction is only flagged.
- Disagreement not fully resolved by applied corrections caps confidence at
  0.4, so the safeguards send the card to review. If every disagreement was
  resolved by an applied correction, confidence becomes the lower of the
  card's and the corrections'.
- The outcome is stored in `identification_json.verification`, including
  `applied` ({field: {from, to}}) and `flagged`. A failed call is stored as
  `{"error": ...}` (a missing provider as `"skipped: ..."`), so it is visible.

**Re-analysis** (`POST /api/cards/{card_id}/reanalyze`): User-triggered
re-identification using the strongest available model: the Claude CLI when
`VISION_PROVIDER=claude_cli`, else the Claude API if its key is set, else
`gemini-3.1-pro-preview`. When that Gemini model is retired or has no allowance
on the plan (the free plan allows Pro models zero requests), it falls back to
`GEMINI_MODEL` instead of failing (`vision.reidentify_strongest()`).

- A paired card's front and back go in one request (`PAIR_USER` prompt).
- A field the back supplied (`pairing.back_supplied_fields`) is kept unless
  the new read is more confident in a different value; a field the new read
  leaves empty keeps its old value. The bbox and side are kept. The new read
  is stored in `identification_json.reanalysis`.
- Works on previews and library cards. A card with a published eBay listing
  is refused with 409. A preview is re-priced with `preview_card()`; a library
  card with `reprice_after_pairing()`, so it is never moved back to preview.

**Hand corrections** (`PATCH /api/cards/{id}`) store one
`IdentificationCorrection` row per changed identity field: the model's read
(from `field_reads`), the value before the edit, the final value and the crop
paths. `tools/export_corrections.py` dumps them as a JSONL golden set.

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
- Edit identity fields manually (`PATCH /api/cards/{id}`): re-prices with
  `preview_card()`, so the card stays in preview; only Add promotes it
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
card_number, parallel, subset, team, rookie, serial_number, condition,
confidence), crop paths
(crop_path, back_crop_path), pricing fields (estimated_price, sold_estimate,
active_estimate, price_basis, derivation, etc.), grading fields
(grade_estimate, gem_mint_score, psa10_candidate), anomaly flags, workflow
status, and relationships to comps/listings.

**IdentificationCorrection**: one hand-corrected identity field (card_id,
field, model_value, previous_value, final_value, crop paths, created_at).

**Comp**: Individual comparable sale tied to a card — title, price, date, source,
graded flag, matching score.

## Key config settings

All in `app/config.py` (set via `.env` file). The app caches settings at startup
via `@lru_cache` — **restart required** after `.env` changes.

| Setting | Default | Purpose |
|---------|---------|---------|
| `VISION_PROVIDER` | auto | auto / anthropic / gemini / claude_cli |
| `CLAUDE_CLI_MODEL` | claude-opus-5-5 | Model for the claude_cli provider |
| `CLAUDE_CLI_TIMEOUT` | 300 | Seconds per photo for claude_cli |
| `ANTHROPIC_MODEL` | claude-opus-5-5 | Detection model (Claude API) |
| `CONFIDENCE_THRESHOLD` | 0.7 | Below this → needs_review |
| `VERIFY_IDENTIFICATION` | true | Second-pass verification |
| `VERIFY_CORRECTION_MIN_CONFIDENCE` | 0.85 | Verifier correction applied at or above this (with a reason) |
| `TWO_PASS_DETECTION` | true | Re-read each card from its crop when a photo has 2+ cards |
| `DETECTION_PASS1_MAX_EDGE` | 2000 | Long edge of the pass-1 copy (boxes only) |
| `TWO_PASS_CONCURRENCY` | 3 | Pass-2 crop reads at once |
| `VISION_MAX_EDGE` | 3000 | Images sent to a provider are downscaled to this |
| `VISION_MAX_BYTES` | 3750000 | ...and re-encoded under this size |
| `CROP_PADDING_PCT` | 0.08 | Margin around each detected card box |
| `SINGLE_CARD_PAD_PCT` | 0.25 | Margin when the photo holds one card |
| `CROP_AUTOSTRAIGHTEN` | false | Deskew the crop (never zooms in) |
| `MIN_STORE_VALUE` | 4.0 | Below this → below_threshold |
| `MIN_EXACT_COMPS` | 3 | Low-comp warning threshold |
| `PRICE_CACHE_TTL_DAYS` | 36525 | Comp cache lifetime |
| `COMP_RECENCY_DAYS` | 90 | Only comps within this window |
| `PRIMARY_SOLD_SOURCE` | sportscardspro | Preferred comp source |
| `COLLECTION_PHOTOS_DIR` | (blank) | Archive folder; blank = disabled |
| `EBAY_MODE` | preview | preview / sandbox / live |
