# Design notes

How the app's screens should look and read, so later changes keep one style.
The approved reference was a static mockup (October 2026) that the current
pages in `app/static/` now follow; when in doubt, match what is there.

## Direction

- A practical personal tool, not a product. Better layout and fewer columns,
  no marketing polish, no onboarding, no illustrations.
- Keep the green identity: dark green header, green primary buttons, mint
  links in the header.
- Show what matters at a glance and push detail one click away (the card
  page, a menu, a tag). The collection table went from 22 columns to 7.
- Plain everyday words in anything a user sees. No long dashes, no AI-ish
  phrasing. Internal ids never show, except the user's own card numbers.
  Server messages pass through `plainReasons()` / `noLongDash()` in
  `common.js` before they are shown.
- Desktop first, and it must still work at 1024px and on a phone with no
  sideways scrolling. Under 860px the collection table becomes the photo grid.
- Light mode only for now.

## Tokens (`app/static/styles.css`, `:root`)

| Token | Value | Use |
|---|---|---|
| `--green` | `#0b3d2e` | header, primary buttons, active chip, selection bar |
| `--green2` | `#14532d` | KPI numbers, big values, button hover |
| `--mint` | `#bfe9d4` | header links, focus ring |
| `--bg` | `#f5f6f8` | page background |
| `--card` | `#fff` | panels, table, tiles |
| `--line` / `--line2` | `#e4e8ec` / `#eef1f3` | borders, row dividers |
| `--ink` / `--mute` | `#1c2126` / `#6b7780` | text, secondary text |
| `--amber` / `--amberbg` | `#8a6100` / `#fdf0cf` | asking-price values, Needs review |
| `--gold` | `#e0a800` | Review count badge, review progress bar |
| `--red` / `--redbg` | `#a3201f` / `#fbdcdc` | source-problem banner, failures, delete |
| `--ok` / `--okbg` | `#0b6b43` / `#d6f5e3` | sold-price values, Ready to list |
| `--blue` / `--bluebg` | `#1d4ed8` / `#dbe7fe` | Live on eBay |
| `--purple` / `--purplebg` | `#4c1d95` / `#e8e0ff` | Sold |
| `--par` / `--parbg` | `#5b2ca0` / `#efe6ff` | parallel tag |
| `--soft` | `#eef2f4` | neutral tags, Under $4 chip, photo placeholders |

Type: the system font stack at 14px; page titles 18px; KPI numbers 22px;
prices 17px bold. Corners: 10px for panels and the table, 999px for chips.

## Shared pieces

- **Header** (every page): Sell Cards, Upload, Review (count of cards
  needing review), Collection, and the eBay mode pill (LIVE eBay, eBay
  sandbox, Preview only). Rendered by `renderHeader()`.
- **Banner**: red box under the header when `GET /api/sources/health`
  returns a `banner` (for example an expired SportsCardsPro sign-in).
- **Status chip** with one line of reason under it: Needs review (amber),
  Ready to list (green), Under $4 (grey), Live on eBay (blue), Sold
  (purple), Listing failed (red). One function, `statusInfo()`.
- **Value**: the price, then "sold · N sales" in green or "asking · N
  listings" in amber, or "no price". Asking prices run high, so amber means
  "less reliable".
- **Menus** (`⋯`) hold the less common actions; **toast** with Undo after
  every delete; **lightbox** for any photo (click to zoom, Esc to close).

## Pages

1. **Collection** (`/repository`): five KPI tiles (value from real sales,
   value from asking prices, need review, live on eBay, sold this month),
   filter chips with counts (All, Needs review, Ready to list, Under $4,
   Listed, Sold, Duplicates, Unmatched backs, Deleted), search, sport and
   tag dropdowns, sort, More filters (value range, possible PSA 10, unusual,
   no price), Check for sales, Refresh all prices, List or Grid. Table
   columns: select, photos, card, condition, value, list at, status, menu.
   Picking rows shows the green selection bar.
2. **Review** (`/review`): progress bar, front and back side by side, why
   the card is here, every field with how sure the read was and which side
   it came from, editable. Looks right (Enter), Re-analyze (R), Skip (right
   arrow), and a menu for Not a card / This is a back.
3. **Upload** (`/`): one drop zone, batch tag, Identify cards; per-photo
   progress; an Options section for grid split, saving to the inbox for
   later, and typing a card in by hand; then "Ready to add" tiles with Add,
   Edit and a menu, plus backs waiting for their front (Pick its front).
4. **Card** (`/card/{id}`): large photos, value and where it came from,
   the price it would list at, the eBay listing (list, change price, end),
   details, every sale found, and how the card was identified.

## Code layout

`common.js` (shared helpers) plus one script per page: `upload.js`,
`collection.js`, `review.js`, `card.js`. Bump the `?v=` on every script and
stylesheet link when a file changes.
