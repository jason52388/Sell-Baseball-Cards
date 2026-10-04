"""Real eBay Sell Inventory API listing client (sandbox/live).

Listing flow per card:
  0. Photos -> eBay Picture Services (media.py; cached, tunnel only as fallback)
  1. PUT  /sell/inventory/v1/inventory_item/{sku}    (createOrReplaceInventoryItem)
  2. POST /sell/inventory/v1/offer                   (createOffer, FIXED_PRICE = BIN)
     or PUT /offer/{offerId} when the SKU already has an offer (retry path)
  3. POST /sell/inventory/v1/offer/{offerId}/publish (publishOffer) -> listingId

After listing:
  - end_offer     : POST /offer/{offerId}/withdraw (ends the live listing)
  - update_price  : GET /offer/{offerId} then PUT /offer/{offerId} (updateOffer)
                    with the new price and matching Best Offer terms

The inventory item and offer bodies come from listing_common's shared builder,
the same one the preview client shows. Every eBay error goes through
_raise_ebay so eBay's own message reaches the user.

Requires a user OAuth token (sell.inventory scope), pre-created business policies
+ inventory location.
"""
from __future__ import annotations

import logging
import time

import httpx

from app.config import REF_IMAGES_DIR, get_settings
from app.services.ebay import media
from app.services.ebay.base import ListingResult
from app.services.ebay.listing_common import (
    best_offer_terms,
    build_lot_payload,
    build_single_payload,
    listing_image_paths,
    reference_image_url,
    set_image_paths,
)
from app.services.ebay.oauth import get_user_access_token

logger = logging.getLogger("ebay.sandbox")


def _raise_ebay(resp: httpx.Response, action: str) -> None:
    """Raise on an eBay error, but include eBay's actual error message (its JSON
    `errors[].longMessage`) instead of a bare status code, and log it."""
    if resp.status_code < 400:
        return
    detail = resp.text
    try:
        errs = resp.json().get("errors") or []
        msgs = [e.get("longMessage") or e.get("message") for e in errs if isinstance(e, dict)]
        detail = "; ".join(m for m in msgs if m) or detail
    except Exception:  # noqa: BLE001
        pass
    logger.error("eBay %s failed (%s): %s", action, resp.status_code, detail)
    raise httpx.HTTPStatusError(
        f"eBay {action} failed: {detail}", request=resp.request, response=resp
    )


SANDBOX_API = "https://api.sandbox.ebay.com"
LIVE_API = "https://api.ebay.com"

# eBay's Inventory service intermittently throws 5xx / errorId 25001 ("Core
# Inventory Service internal error") that succeeds on a retry. Retry idempotent
# create/replace + publish calls a few times with exponential backoff.
_RETRY_STATUSES = {500, 502, 503, 504}
_MAX_ATTEMPTS = 3

# Fields updateOffer accepts (it REPLACES the offer, so everything we keep must
# be sent back). Read-only fields from getOffer (offerId, status, listing...)
# are dropped.
_UPDATE_OFFER_FIELDS = (
    "availableQuantity", "categoryId", "charity", "extendedProducerResponsibility",
    "hideBuyerDetails", "includeCatalogProductDetails", "listingDescription",
    "listingDuration", "listingPolicies", "listingStartDate", "lotSize",
    "merchantLocationKey", "pricingSummary", "quantityLimitPerBuyer", "regulatory",
    "secondaryCategoryId", "storeCategoryNames", "tax",
)


def _send_with_retry(send, *args, **kwargs):
    """Call an httpx request method, retrying on transient 5xx responses."""
    resp = None
    for attempt in range(_MAX_ATTEMPTS):
        resp = send(*args, **kwargs)
        if resp.status_code not in _RETRY_STATUSES:
            return resp
        if attempt < _MAX_ATTEMPTS - 1:
            logger.warning(
                "eBay %s -> %s (attempt %d/%d); retrying",
                resp.request.url, resp.status_code, attempt + 1, _MAX_ATTEMPTS,
            )
            time.sleep(0.5 * (2 ** attempt))
    return resp


class MissingCredentialsError(RuntimeError):
    pass


class SandboxEbayClient:
    def __init__(self, live: bool = False) -> None:
        self.live = live
        self.api_base = LIVE_API if live else SANDBOX_API

    def _require_config(self, s) -> None:
        missing = [
            name
            for name, val in {
                "EBAY_CLIENT_ID": s.ebay_client_id,
                "EBAY_CLIENT_SECRET": s.ebay_client_secret,
                "EBAY_USER_REFRESH_TOKEN": s.ebay_user_refresh_token,
                "EBAY_FULFILLMENT_POLICY_ID": s.ebay_fulfillment_policy_id,
                "EBAY_PAYMENT_POLICY_ID": s.ebay_payment_policy_id,
                "EBAY_RETURN_POLICY_ID": s.ebay_return_policy_id,
                "EBAY_MERCHANT_LOCATION_KEY": s.ebay_merchant_location_key,
            }.items()
            if not val
        ]
        if missing:
            raise MissingCredentialsError(
                "eBay listing requires: " + ", ".join(missing)
            )

    def _token(self) -> str:
        return get_user_access_token(live=self.live)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json",
            "Content-Language": "en-US",
        }

    # --- photos -----------------------------------------------------------------

    def _photos(self, s, paths: list[str], card=None) -> list[str]:
        urls = media.resolve_image_urls(paths, s, self.live, self._token, required=self.live)
        if card is not None and s.ebay_include_reference_image:
            ref = getattr(card, "reference_image_url", None) or ""
            if ref.startswith("/refimg/"):
                local = REF_IMAGES_DIR / ref.removeprefix("/refimg/")
                urls += media.resolve_image_urls(
                    [str(local)], s, self.live, self._token, required=False
                )
            elif ref.startswith("https://"):
                urls.append(reference_image_url(card, s.public_image_base_url))
        if self.live and not urls:
            raise MissingCredentialsError(
                "Live eBay listings require at least one photo, and this card has none."
            )
        return urls

    # --- listing ----------------------------------------------------------------

    def create_listing(self, card, list_price: float) -> ListingResult:
        s = get_settings()
        self._require_config(s)
        image_urls = self._photos(s, listing_image_paths(card), card)
        payload = build_single_payload(card, list_price, s, image_urls)
        sku = payload["sku"]

        headers = self._headers()
        with httpx.Client(base_url=self.api_base, timeout=60) as client:
            r1 = _send_with_retry(
                client.put,
                f"/sell/inventory/v1/inventory_item/{sku}",
                headers=headers,
                json=payload["inventory_item"],
            )
            _raise_ebay(r1, "inventory item")

            offer_id, _created, _status = self._get_or_create_offer(
                client, headers, sku, payload["offer"]
            )
            listing_id = self._publish(client, headers, offer_id)

        return ListingResult(
            sku=sku,
            offer_id=offer_id,
            listing_id=listing_id,
            status="published",
            list_price=list_price,
            response={"offerId": offer_id, "listingId": listing_id, "imageUrls": image_urls},
            message=f"Published to eBay {'live' if self.live else 'sandbox'}.",
        )

    def create_set_listing(self, cards: list, list_price: float) -> ListingResult:
        """Combine multiple cards into ONE eBay lot listing (all cards + photos).

        A lot's SKU is a hash of its card ids, so a retry with a different
        selection gets a new SKU. If any step fails, the inventory item and offer
        this attempt created are deleted so they are not left orphaned on eBay.
        """
        s = get_settings()
        self._require_config(s)
        image_urls = self._photos(s, set_image_paths(cards))
        payload = build_lot_payload(cards, list_price, s, image_urls)
        sku = payload["sku"]

        headers = self._headers()
        offer_id = None
        created = False
        existing_status = None
        with httpx.Client(base_url=self.api_base, timeout=60) as client:
            try:
                r1 = _send_with_retry(
                    client.put,
                    f"/sell/inventory/v1/inventory_item/{sku}",
                    headers=headers,
                    json=payload["inventory_item"],
                )
                _raise_ebay(r1, "lot inventory item")
                offer_id, created, existing_status = self._get_or_create_offer(
                    client, headers, sku, payload["offer"]
                )
                listing_id = self._publish(client, headers, offer_id)
            except Exception:
                self._cleanup_lot(
                    client, headers, sku, offer_id if created else None, existing_status
                )
                raise

        return ListingResult(
            sku=sku,
            offer_id=offer_id,
            listing_id=listing_id,
            status="published",
            list_price=list_price,
            response={"offerId": offer_id, "listingId": listing_id, "cards": len(cards)},
            message=f"Published {len(cards)}-card lot to eBay "
            f"{'live' if self.live else 'sandbox'} ({len(image_urls)} photo(s)).",
        )

    def _publish(self, client, headers, offer_id: str) -> str | None:
        r = _send_with_retry(
            client.post, f"/sell/inventory/v1/offer/{offer_id}/publish", headers=headers,
        )
        _raise_ebay(r, "publish")
        return r.json().get("listingId")

    def _cleanup_lot(self, client, headers, sku, created_offer_id, existing_status) -> None:
        """Best-effort removal of what a failed lot attempt left behind. Never
        touches an offer that was already PUBLISHED before this attempt."""
        if existing_status == "PUBLISHED":
            return
        try:
            if created_offer_id:
                client.delete(f"/sell/inventory/v1/offer/{created_offer_id}", headers=headers)
            # Deleting the inventory item also removes any unpublished offer on it.
            client.delete(f"/sell/inventory/v1/inventory_item/{sku}", headers=headers)
            logger.info("cleaned up failed lot %s", sku)
        except Exception:  # noqa: BLE001
            logger.exception("cleanup of failed lot %s failed", sku)

    def _get_or_create_offer(self, client, headers, sku, payload) -> tuple[str, bool, str | None]:
        """Reuse an existing offer for this SKU if present, else create one.
        Returns (offer_id, created_now, existing_offer_status)."""
        existing = client.get(
            "/sell/inventory/v1/offer", headers=headers, params={"sku": sku}
        )
        if existing.status_code == 200:
            offers = existing.json().get("offers", [])
            if offers:
                offer_id = offers[0]["offerId"]
                r = _send_with_retry(
                    client.put,
                    f"/sell/inventory/v1/offer/{offer_id}",
                    headers=headers,
                    json=payload,
                )
                _raise_ebay(r, "update offer")
                return offer_id, False, offers[0].get("status")

        created = _send_with_retry(
            client.post, "/sell/inventory/v1/offer", headers=headers, json=payload,
        )
        _raise_ebay(created, "create offer")
        return created.json().get("offerId"), True, None

    # --- after listing ------------------------------------------------------------

    def end_offer(self, offer_id: str) -> None:
        """End a live listing (withdrawOffer). The offer and inventory item stay,
        so the card can be re-listed later under the same SKU."""
        with httpx.Client(base_url=self.api_base, timeout=60) as client:
            r = _send_with_retry(
                client.post, f"/sell/inventory/v1/offer/{offer_id}/withdraw",
                headers=self._headers(),
            )
            _raise_ebay(r, "end listing")

    def update_price(self, offer_id: str, new_price: float) -> None:
        """Change a live listing's price (updateOffer), with Best Offer terms
        recomputed for the new price so auto-accept never exceeds it."""
        s = get_settings()
        headers = self._headers()
        with httpx.Client(base_url=self.api_base, timeout=60) as client:
            got = client.get(f"/sell/inventory/v1/offer/{offer_id}", headers=headers)
            _raise_ebay(got, "read offer")
            current = got.json()
            body = {k: current[k] for k in _UPDATE_OFFER_FIELDS if k in current}
            body["pricingSummary"] = {
                **(current.get("pricingSummary") or {}),
                "price": {"value": f"{new_price:.2f}", "currency": "USD"},
            }
            policies = dict(current.get("listingPolicies") or {})
            terms = best_offer_terms(new_price, s)
            if terms:
                policies["bestOfferTerms"] = terms
            else:
                policies.pop("bestOfferTerms", None)
            body["listingPolicies"] = policies
            r = _send_with_retry(
                client.put, f"/sell/inventory/v1/offer/{offer_id}", headers=headers, json=body,
            )
            _raise_ebay(r, "update price")
