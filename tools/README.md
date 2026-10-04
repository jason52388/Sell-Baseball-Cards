# Bulk tools

## `recrop_rotated.py`: re-cut crops sliced by the sideways-photo bug

Before the vision model was sent upright photos, every phone photo's card box
was read off the sideways pixels, so crops cut cards off. This tool turns each
stored box upright and re-cuts the front and back crops from the original
photos with today's padding rules. No model calls. It covers cards in preview
and in the library whose photo is still in `data/inbox/processed/`, skips cards
with a published eBay listing, keeps the old crop files, and writes an undo log
(`data/recrop-undo-*.json`). Safe to re-run: crops it already redid are skipped.

```bash
cd $GITHUB_DIR/Sell-Baseball-Cards
.venv/bin/python -m tools.recrop_rotated --data-dir data --preview /tmp/recrop.jpg
.venv/bin/python -m tools.recrop_rotated --data-dir data --apply
```

Look at the preview sheet (old crop left, new crop right) before `--apply`. A
card whose box was simply wrong will still be cut after this; use Re-analyze on
it in the app, which now reads the upright photo.

## `verify_130point.py` — sanity-check the 130point sold-comp source

130point has no official API, so the source scrapes its results page. This tool
runs one real search and prints what the parser extracts, so you can confirm the
selectors still match the live markup after enabling `POINT130_ENABLED`.

```bash
python -m tools.verify_130point "1989 Upper Deck Ken Griffey Jr #1"
python -m tools.verify_130point "..." --raw    # dump raw HTML if 0 comps parse
```

Needs outbound network (won't run inside a locked-down web session). If it
prints 0 comps but the site shows results in a browser, the markup changed —
adjust `parse_results_html` in `app/services/point130.py`.

## `verify_sportscardspro.py` — sanity-check the SportsCardsPro sales scrape

The SportsCardsPro/PriceCharting API returns only *aggregate* prices. Individual
sales are scraped from the product web page. This tool runs the real product
lookup, prints the aggregate grade tiers, then fetches and parses the page's
recent-sales table so you can confirm the URL shape and selectors.

```bash
python -m tools.verify_sportscardspro "1989 Upper Deck Ken Griffey Jr #1"
python -m tools.verify_sportscardspro "..." --raw    # dump page HTML if 0 sales parse
```

Needs `PRICECHARTING_TOKEN` and outbound network. If 0 sales parse but the page
shows them, fix `parse_sales_table_html` / `product_page_url` in
`app/services/pricecharting.py`.

## `ingest_folder.sh` — identify a folder of photos with your Claude subscription

Run hundreds of card photos through **Claude Code** (billed to your Claude
subscription, **no `ANTHROPIC_API_KEY` needed by the app**) and push the results
into the running website. Only the *identification* moves off-box — the app still
does the cropping, eBay pricing, reference-photo lookup, and the
preview → review → add flow exactly as it does for in-app photo uploads.

Claude is shown an upright copy of each photo (`tools/upright_copy.py`), made in
a temporary `data/.ingest_view.*` folder that is removed when the run ends.
Phones store pixels sideways with a flag saying how to turn them, and the
cropper turns the photo upright before cutting, so the card boxes must be read
off the upright view or the crops slice through the cards. The original photo
is still what gets uploaded, so its timestamp keeps front/back pairing working.

### How it works

```
"Queue for Claude" button ─▶ POST /api/queue ─▶ data/inbox/*.jpg
data/inbox/*.jpg ──▶ claude -p (reads each photo, returns detection JSON)
                 ──▶ curl POST /api/ingest (image + JSON)
                 ──▶ app crops + prices each card ──▶ "preview" in the repository
                 ──▶ photo moved to data/inbox/processed/
```

The website's **Queue for Claude** button drops photos into `data/inbox` with no
AI call (so it never hits a vision rate limit). This script then identifies them
on your Claude subscription. With **no folder argument it processes that inbox**
and moves each finished photo to `data/inbox/processed/` so re-runs don't
duplicate. You can also point it at any other folder.

The script pulls its detection prompt from the app's own
`app/prompts/card_detection.py`, so the JSON schema the model emits always matches
what `/api/ingest` expects.

### Prerequisites

- The app is running: `./run.sh` (default `http://127.0.0.1:8000`).
- [Claude Code](https://claude.com/claude-code) installed and logged in to your
  Claude subscription (`claude` on your PATH).
- Run from the project root (so the venv and prompt module resolve).

### Usage

```bash
tools/ingest_folder.sh                       # process the website's queue (data/inbox)
tools/ingest_folder.sh ~/card-photos         # or any folder
tools/ingest_folder.sh ~/card-photos http://127.0.0.1:8000   # custom server URL
```

It prints `OK`/`FAIL` per photo and a summary. Then open the app at `/`, review
each detected card next to its **marketplace reference photo**, and **Add** the
ones you want — they move into the repository just like uploaded cards. Low-
confidence cards can still be re-analyzed or entered manually there.

### Notes / limits

- **Subscription, not unlimited.** Large runs pace against Claude Code's usage
  limits; the script processes one photo per `claude` invocation so it can be
  re-run safely and resumes where your limits allow.
- **No second-pass verification.** The in-app upload path runs a server-side
  verification call (which needs an API key); ingest skips it. The per-field
  confidence Claude returns still drives the review safeguards.
- **Photos stay local.** Only the extracted text/identity is sent to the app.
  (Listing on *live* eBay still needs publicly reachable image URLs —
  `PUBLIC_IMAGE_BASE_URL` — as documented in the main README.)
- **Permissions.** The script passes `--allowedTools Read` so headless Claude
  Code can open each image without an interactive prompt.
