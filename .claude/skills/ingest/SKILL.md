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
| `POST /api/upload` | `upload()` | Interactive: save photos, return a background job at once |
| `POST /api/queue` | `queue_photos()` | Drag-drop to inbox for async processing later |
| `POST /api/ingest` | `ingest()` | External: pre-identified cards with JSON detections (synchronous, one photo per call) |

Every upload and ingest saves the original into `data/inbox/processed/` under a
unique, path-safe name (`save_original`, `_safe_inbox_name`) and records it on
an `ImageUpload` row: `filename` (original, for display), `stored_name` (the
file on disk; archive and `tools/recrop_rotated.py` read it) and `sha256`. A
photo whose bytes match an earlier upload that did not fail (and whose cards
were not all deleted) is skipped: the upload job reports "already uploaded
(cards #12 ...)" and ingest answers 409, unless `force=true`. HEIC photos are
converted to JPEG first (`app/services/images.py`: pillow-heif if installed,
else macOS `sips`, else the photo fails with "HEIC not supported here, export
the photo as JPG"). An optional `batch_tag` form param groups cards.

File locations:
- Originals: `data/inbox/` (queued, not yet identified), `data/inbox/processed/`
  (uploaded or ingested), `data/inbox/duplicates/` (repeat uploads parked for
  a forced retry, cleared after 7 days)
- Crops go to `data/crops/`

### Background jobs (`app/services/jobs.py`, `app/routers/jobs.py`)

`POST /api/upload` creates a `Job` (kind `upload`) with one `JobItem` per photo
and returns `jobs.serialize(job)` at once. ONE daemon worker thread runs waiting
items oldest first, one at a time (the vision CLI and SQLite never do two at
once). Handlers are registered per kind (`jobs.register`): `upload` in
upload.py (`_jobs_upload_handler` -> `_process_image`), `reprice` in cards.py
(`POST /api/cards/reprice`, one item per library card). A handler reports
progress with `progress("Pricing card 2 of 6")`, which commits; raising
`jobs.ItemFailed` fails the item with that message.

- `GET /api/jobs/{id}`: `{job_id, kind, status: queued|running|done, total,
  done, failed, photos: [{index, filename, state: waiting|working|done|failed,
  step, message, card_ids, upload_id, card_id, duplicate, can_retry}]}`
- `GET /api/jobs/active`: unfinished jobs, plus jobs finished in the last day
  with failed items until `POST /api/jobs/{id}/dismiss`
- `POST /api/jobs/{id}/retry/{index}[?force=true]`: re-queue a failed item; a
  repeat upload needs force. An upload retry first deletes the preview cards
  the failed attempt left.
- Startup (`main.startup_tasks`): `jobs.recover()` fails items left `working`
  ("Interrupted by a server restart"), purges the trash, and wakes the worker
  if items are waiting.
- Tests set `jobs.INLINE = True` (conftest) so `kick()` runs the queue inside
  the request.

**Write transactions stay short.** `_cards_from_detections` commits after each
step: (1) cards + crops, (2) pairing, (3) per front: verify, commit, price,
commit, (4) re-price fronts elsewhere that gained one of this photo's backs.
Pricing runs every comp fetch before touching a Comp row, and with
`commit_after_fetch=True` commits the comp-cache writes right after, so the
SQLite write lock is never held across a vision or network call.

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
- `remember_back_source()` records the back's own `_upload_id`,
  `_stored_name`, `_source_filename`, `_photo_taken_at` and `_batch_tag` in the
  front's `back_identification_json`; unmatch restores them on the recreated
  back, so it archives its own photo and can still re-pair by timestamp
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
- `POST /api/cards/{front_id}/attach-back/{back_id}[?confirm=true]`
- `POST /api/cards/{a_id}/pair/{b_id}[?confirm=true]`
- `POST /api/cards/{card_id}/mark-back[?confirm=true]`
- `POST /api/cards/{front_id}/detach-back` (unmatch)

The card used as the back is merged away, so `_guard_consumed` refuses (409) a
card live or sold on eBay, a deleted card, or one with its own back attached,
and a card already in the library needs `confirm=true`. Attaching a back to a
front that already has one splits the old back off as an orphan first
(`_split_off_back`, the unmatch logic), never deleting its image. Automatic
pairing skips deleted cards and a front that already has a back.

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

**Comp gathering** (`app/services/comp_sources.py`, `collect_comps()` /
`gather_comps()`): pricing passes its own `db` session so the comp cache writes
inside the request's transaction (a second SQLite connection waited on the
upload's write lock and failed with "database is locked").

| Source | Type | Config | Status key |
|--------|------|--------|------------|
| eBay Marketplace Insights | Sold | `EBAY_INSIGHTS_ENABLED` | `insights` |
| SportsCardsPro / PriceCharting | Sold (market averages + grade tiers) | `PRICECHARTING_TOKEN` | `sportscardspro` |
| SportsCardsPro recent sales | Sold (individual) | `SPORTSCARDSPRO_SALES_ENABLED` | `sportscardspro_sales` |
| 130point.com | Sold (incl. best-offer) | `POINT130_ENABLED` | `130point` |
| eBay headless scrape | Sold | `EBAY_BROWSER_SCRAPE_ENABLED` | `ebay_browser_scrape` |
| eBay Browse API | Active asking (price + cheapest shipping) | eBay keyset (free) | `ebay_browse` |
| Web search | Fallback | `WEBSEARCH_API_KEY` | |

**Source status**: each source runs in isolation and yields a status: `ok`,
`empty`, or a failure (`error`, `auth_expired`, `unauthorized`, `blocked`,
`quota`). A failure is never raised into the card. Its note starts with
`Price source problem: ` (`comp_sources.SOURCE_PROBLEM_PREFIX`); pricing keeps
those segments FIRST in `review_reason` through `_route_status` and
`finalize_card`'s gate, and when no price was found leads with them instead of
"verify the card's year/set/insert". The app-level registry is served by
`GET /api/sources/health` (`app/routers/sources.py`): per source the last
state, message, last success and last error, plus a `banner` string. It is kept
in memory and persisted as a reserved `price_cache` document
(`__source_health__`). Insights switched off on purpose adds no note. 130point
reports a Cloudflare challenge, HTTP 403/429/503 or its empty stub response as
`blocked`.

**Cache** (`app/services/comp_cache.py`): per (query, graded, marketplace).
Sold-only entries live `PRICE_CACHE_TTL_DAYS` (30); entries holding asking
prices or a SportsCardsPro average live `PRICE_CACHE_ACTIVE_TTL_DAYS` (7).
Empty results and results where any source failed are never cached. Dated
individual sales accumulate across refreshes; asking prices, undated comps and
market averages are snapshots replaced on each fetch.

**Scoring** (`app/services/matching.py`, `score_comp()`): Each comp is scored
as exact, near, graded, or excluded. This one file decides which prices count,
so its rules are deliberately strict:

- **excluded** first, with a reason: junk listings (`junk_reason()`: lots,
  "x10", "(10)", bundles, you-pick, reprints/RP, customs, ACEO, art cards,
  digital/NFT/Topps Bunt, breaks, facsimile, replica), a different player, or a
  parallel the card lacks (`parallel_markers()`: Gold, Refractor, Prizm, Holo,
  Foil, Chrome, Xfractor, Atomic, Sapphire, Black, "parallel", serial "/NN" or
  "1/1", printing plate, SP/SSP, variation). Markers in the card's own
  set/parallel/subset/player are allowed ("Topps Gold Label", "Topps Chrome");
  "Gold Glove" is not a parallel
- **exact** = player + any 2 of year/set/number (any two: a card with no year
  still prices off set + number)
- All token matching is **whole-word**: substring matching let "Bo" match inside
  "Bob" and the year "1989" match inside "219890"
- **Set** requires every significant word ("Topps Chrome" is not "Topps")
- **Parallel**, when the card has one, must appear in the title or the comp
  cannot be exact: a base sale is worth a fraction of its /50 parallel
- **Subset** (optional `card.subset`, e.g. "League Leaders") is a bonus signal
  in the reason, never required
- **graded** uses the ONE shared `matching.GRADE_RE` (pricecharting, point130
  and the eBay scrapers import it). It accepts grade words between grader and
  number ("PSA Gem Mint 10", "BGS Pristine 10", "SGC 9.5 Mint+"), refuses
  "PSA/DNA", and a condition of plain "Graded" (eBay Browse) also counts

**SportsCardsPro product picker** (`pricecharting.select_best_product()`): the
product must carry the card's year, number (when known), the player's last name
(`require_player`; used for the price AND the reference photo), and no parallel
marker the query lacks. Scraped recent-sales rows carry the product title, and
rows from a graded tier table are flagged graded.

**Graded estimate**: only comps that scored `graded` feed
`graded_value_estimate`. Sources answer the graded query with raw sales mixed
in, so counting everything understated the PSA 10 upside badly.

**Estimate** (`_sold_pool()`): median of the outlier-trimmed pool of ALL sold
sources. The same sale from two sources counts once (`PRIMARY_SOLD_SOURCE` is
only the tie-break). Market averages (source `sportscardspro`) carry their fetch
date so the `COMP_RECENCY_DAYS` window applies, and are labelled as averages,
not sales. Undated sales never count as recent: they are used only when no
dated recent sale exists, and are labelled. Fewer than `MIN_EXACT_COMPS`
individual sales is stated in the derivation and noted low-confidence.
`sold_max_estimate` is the max of the trimmed set. Prefers SOLD over ACTIVE.

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
- Discard (soft delete: `DELETE /api/cards/{id}`, restorable for 7 days with
  `POST /api/cards/{id}/restore`; see "Deleting" below)
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
- `cards._archive_uploads()` moves each touched source photo (the front's and
  its back's) out of the inbox, but only once no front from that upload is
  still in preview (best-effort)
- `photo_archive.archive_crop_files()` copies crops to the collection folder

Returns `{"added": [CardOut...], "skipped": [{"id", "reason"}]}`: only cards
whose status actually changed are added; a library, deleted, missing or back
card is listed in `skipped` with the reason.

Card is now in the library, visible in the collection view.

## Deleting

`DELETE /api/cards/{id}[?confirm=true]` is a soft delete (`app/services/trash.py`):
status `deleted`, `deleted_at` stamped, the old status kept in
`status_before_delete`. Deleted cards are hidden from every list, count,
pairing search and the review queue; `GET /api/cards?status=deleted` is the
trash. Restore within `TRASH_RETENTION_DAYS` (7); after that 410, and
`trash.purge_expired` removes the row and crop files on the next startup. A
card live on eBay has its listing ended first (`orders.end_listing_for_card`);
if eBay refuses, 502 and nothing is deleted. A sold card needs `confirm=true`.

## Review queue (`app/routers/review.py`)

- `GET /api/review/next?after_id=`: next `needs_review` library front in id
  order (wrapping), with `review_reason`, `fields` (per identity field:
  `value`, `confidence`, `side`: front / back / both / verifier / user /
  null, plus the raw `front_read` and `back_read`), crop URLs, `remaining`
  and `next_id`.
- `POST /api/cards/{id}/confirm`: confidence 1.0, `user_confirmed` stored in
  `identification_json` (no correction row), identity reasons dropped from
  `review_reason`, then `finalize_card` (or `price_card` when the card was
  never priced). Returns `{card, next_id, remaining}`.

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
— cleans up the working inbox. A photo moves once none of its cards is still
in preview (all added or discarded, at least one added), and is named
neutrally: batch tag or upload date plus the original stem
(`photo_archive.source_photo_label`, e.g. `box 7 IMG_0042.jpg`), since one photo
holds up to nine cards. Backs recorded before back audits kept `_upload_id` are
moved under their stored name.

**Crop images** (front + back) are COPIED to the collection folder — the app
still needs the originals in `data/crops/`.

Crops are renamed to match the card description:
`{Player}, {Manufacturer}, {Year}, {Parallel} (front).jpg`

Example: `Mike Trout, Topps Chrome, 2023, Refractor (front).jpg`

Duplicate filenames get a numeric suffix. Failures are logged but never block
card addition.

## Key data model

`app/models.py`

**ImageUpload**: original filename, stored_name (file in data/inbox/processed),
sha256, card_count, batch_tag, uploaded_at, error

**Job / JobItem**: background work (kind `upload` or `reprice`); per item:
filename, state, step, message, card_ids_json, upload_id or card_id,
duplicate + staged_path for a skipped repeat upload

**Card**: Full card record with identity fields (player, year, sport, set_brand,
card_number, parallel, subset, team, rookie, serial_number, condition,
confidence), crop paths
(crop_path, back_crop_path), pricing fields (estimated_price, sold_estimate,
active_estimate, price_basis, derivation, etc.), grading fields
(grade_estimate, gem_mint_score, psa10_candidate), anomaly flags, workflow
status (plus `deleted_at` / `status_before_delete` for the trash), and
relationships to comps/listings. Properties `listing_state`,
`suggested_list_price` and `price_floor` feed `CardOut`.

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
| `PRICE_CACHE_TTL_DAYS` | 30 | Cache lifetime for sold comps |
| `PRICE_CACHE_ACTIVE_TTL_DAYS` | 7 | Cache lifetime when asking prices / averages are cached |
| `COMP_RECENCY_DAYS` | 90 | Only comps within this window |
| `PRIMARY_SOLD_SOURCE` | sportscardspro | Dedupe tie-break when two sources report one sale |
| `COLLECTION_PHOTOS_DIR` | (blank) | Archive folder; blank = disabled |
| `EBAY_MODE` | preview | preview / sandbox / live |
