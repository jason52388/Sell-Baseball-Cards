# Listing cards on eBay

How the app turns a card in the collection into an eBay Buy-It-Now listing,
and what happens after: changing the price, ending the listing, and learning
that it sold.

Code: `app/services/ebay/listing_common.py` (what is sent), `sandbox.py` (the
real Sell API calls, sandbox or live), `preview.py` (builds the same payload,
sends nothing), `media.py` (photos), `orders.py` (state, ending, sold sync),
`app/routers/listings.py` (endpoints).

## What gets sent

`EBAY_MODE=preview` builds the payload and sends nothing. `sandbox` and `live`
send it. Both use the same builder (`build_single_payload` /
`build_lot_payload`), so a preview shows exactly what a publish would send. The
only difference is the photo addresses: a real publish uploads the photos to eBay
first and sends eBay's own addresses.

### Condition

eBay's trading-card categories accept only two conditions:

| Card | Condition sent | Descriptors |
| --- | --- | --- |
| Raw (ungraded), any wear | `USED_VERY_GOOD` (Ungraded, 4000) | Card Condition (40001): Near mint or better / Excellent / Very good / Poor |
| In a grading slab | `LIKE_NEW` (Graded, 2750) | Professional Grader (27501) + Grade (27502) |

A card counts as slabbed only when its **condition** text names a grader and a
grade, such as `PSA 9` or `BGS 9.5`. A grade *estimate* (`grade_estimate`,
`psa10_candidate`) is a guess about a raw card and never makes it graded. A lot
is described by its worst card's condition.

### Title

Built in this order, keeping whole words only, up to eBay's 80 characters:
year, set, player, insert set, parallel, `#number`, print run (`/99`), `RC`,
team, then the sport word. A piece that does not fit is skipped; the set and
player keep as many leading words as fit.

### Item specifics

Sport, Type, Player/Athlete, Card Name, Manufacturer (from the set: Topps and
Bowman are Topps; Donruss, Prizm and Optic are Panini; also Upper Deck, Fleer,
Score, Pinnacle, Pacific, Leaf), Set, Season, Year Manufactured, Card Number,
Parallel/Variety, Insert Set, Team, League, Print Run, Features (Rookie, Serial
Numbered, Parallel/Variety), Graded, Professional Grader and Grade (slabs only),
Autographed, Vintage (before 1980), Original/Licensed Reprint. Empty values are
left out and every value is cut to 65 characters.

Insert set, team and rookie come from optional card fields (`subset`, `team`,
`rookie`); the builder works whether or not those fields exist yet.

### Photos

The front crop, then the back crop when the card has one. The marketplace
reference photo is another seller's picture, so it is left out unless
`EBAY_INCLUDE_REFERENCE_IMAGE=true`.

Each photo is uploaded to eBay Picture Services (Commerce Media API,
`createImageFromFile`; sandbox host `apim.sandbox.ebay.com`). eBay's address for
it is remembered in `data/ebay_image_cache.json` (per file, size and modified
time), so a retry does not upload again. Only if an upload fails does the app use
the tunnel address (`PUBLIC_IMAGE_BASE_URL/crops/...`), and only after checking
that it is https, answers 200 and returns an image. If not, a live listing stops
with a message saying which photo failed and to start the tunnel.

## List price

`suggested_list_price(card, settings)`:

1. Base price by where the estimate came from (`price_basis`):
   - `sold` (what cards actually sold for): estimate x `PRICE_MARKUP` (1.15)
   - `active` (what sellers are asking): median ask x `EBAY_ASK_UNDERCUT` (0.95).
     Asking prices already sit above sold prices, so a markup on top would price
     the card out of the market.
2. Floor: the lowest price that still nets `EBAY_MIN_NET` (0.50) after eBay's
   fee (`EBAY_FEE_PCT`), the per-order fee (`EBAY_PER_ORDER_FEE`) and shipping
   supplies (`EBAY_SHIPPING_SUPPLIES_COST`, 1.00). With the defaults that is
   $2.20. The price never goes below it.
3. Rounded to the nearest .99 at or above the floor.

A lot adds up its cards' base prices and applies the floor once
(`suggested_lot_price`). A price you type yourself is refused if it is below the
floor.

Best Offer auto-accepts at `EBAY_BEST_OFFER_AUTO_ACCEPT_PCT` (0.80) of the list
price, never below the floor, and auto-declines offers under the floor. When the
list price is at the floor there is no room, so Best Offer is off.

## Endpoints

| Endpoint | What it does | Answer |
| --- | --- | --- |
| `POST /api/listings/sell` `{card_ids, prices?}` | List each card on its own | `{results: [{card_id, status, ok, error, listing_url, listing_id, list_price, message}]}` |
| `POST /api/cards/{id}/list` | List one card | `SellResult`; **409** if already live or sold |
| `POST /api/listings/sell-set` `{card_ids, prices?: {set}}` | One lot listing | `SetSellResult`; `status: "blocked"` if any card is already live or sold |
| `GET /api/listings/{id}` | Listing state for the UI | `{card_id, listing_state, listing_url, list_price, suggested_list_price, price_floor, ebay_mode, sku, offer_id, listing_id, sold_at, sold_price}` |
| `POST /api/listings/{id}/price` `{price?}` | Change a live listing's price (blank = suggested) | listing info + `{ok, message}`; 400 below the floor, 409 not live, 502 eBay refused |
| `POST /api/listings/{id}/end` | End (withdraw) a live listing; a lot ends for every card | listing info + `{ended, card_ids, message}`; 409 not live |
| `POST /api/listings/sync-sold` | Ask eBay which listings sold | `{orders_checked, sold: [{card_id, order_id, sold_price, sold_at}], since, errors}` |

`listing_state` is `none`, `live`, `ended` or `sold`. A card that is live is
never listed a second time: change its price instead, or end it first. An ended
card can be listed again.

## Sold sync

`sync_sold` reads orders through the Fulfillment API (`getOrders`, filtered by
last-modified date from just before the oldest live listing, at most two years
back), matches each line item to a listing by SKU or by eBay item number, skips
cancelled orders, and marks the listing `sold` with the order date, price and
order id. For a lot, every card's row carries the whole lot's sale price.

This needs the `sell.fulfillment` permission. The consent link
(`/ebay/oauth/start`) now asks for it, but a refresh token made before that does
not have it: **re-authorize once at `/ebay/oauth/start`**. Until then the sold
sync answers with an error explaining this, and listing keeps working (listing
calls do not ask for the new permission).

## Tokens

User access tokens are kept until shortly before they expire instead of being
fetched for every card. The consent callback saves the new refresh token and its
expiry date (`EBAY_USER_REFRESH_TOKEN_EXPIRES_AT`) to `.env` and reloads
settings, so no restart is needed. A warning is logged when the refresh token has
30 days or less left.

## For other parts of the app

- `listing_common.suggested_list_price(card, settings)`: the list price to show.
- `orders.listing_state(card)`: `none` / `live` / `ended` / `sold`.
- `orders.end_listing_for_card(db, card)`: end a card's live listing; call it
  before deleting a listed card. Raises if eBay refuses.
