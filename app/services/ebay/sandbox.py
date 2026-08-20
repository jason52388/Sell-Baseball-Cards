"""Real eBay Sell Inventory API listing client (sandbox/live).

Listing flow per card:
  1. PUT  /sell/inventory/v1/inventory_item/{sku}    (createOrReplaceInventoryItem)
  2. POST /sell/inventory/v1/offer                   (createOffer, FIXED_PRICE = BIN)
  3. POST /sell/inventory/v1/offer/{offerId}/publish (publishOffer) -> listingId

Requires a user OAuth token (sell.inventory scope), pre-created business policies
+ inventory location, and (for live) a publicly reachable image URL per card.
"""
from __future__ import annotations

import logging
import time

import httpx

from app.config import get_settings
from app.services.ebay.base import ListingResult
from app.services.ebay.listing_common import (
    build_aspects,
    build_condition_descriptors,
    build_description,
    build_set_aspects,
    build_set_description,
    build_set_title,
    build_title,
    card_image_urls,
    map_condition,
    set_image_urls,
    set_sku,
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

    def _image_urls(self, s, card) -> list[str]:
        return card_image_urls(card, s.public_image_base_url)

    def _headers(self) -> dict[str, str]:
        token = get_user_access_token(live=self.live)
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Content-Language": "en-US",
        }

    def create_listing(self, card, list_price: float) -> ListingResult:
        s = get_settings()
        self._require_config(s)
        sku = f"CARD-{card.id}"
        image_urls = self._image_urls(s, card)
        if self.live and not image_urls:
            raise MissingCredentialsError(
                "Live eBay listings require an image. Set PUBLIC_IMAGE_BASE_URL so "
                "the card crop is reachable by eBay."
            )

        product: dict = {"title": build_title(card), "aspects": build_aspects(card)}
        if image_urls:
            product["imageUrls"] = image_urls
        inventory_payload = {
            "product": product,
            "condition": map_condition(card),
            "conditionDescriptors": build_condition_descriptors(card),
            "availability": {"shipToLocationAvailability": {"quantity": 1}},
        }

        headers = self._headers()
        with httpx.Client(base_url=self.api_base, timeout=60) as client:
            r1 = _send_with_retry(
                client.put,
                f"/sell/inventory/v1/inventory_item/{sku}",
                headers=headers,
                json=inventory_payload,
            )
            _raise_ebay(r1, "inventory item")

            offer_id = self._get_or_create_offer(
                client, headers, s, sku, list_price,
                description=build_description(card),
            )

            r3 = _send_with_retry(
                client.post,
                f"/sell/inventory/v1/offer/{offer_id}/publish",
                headers=headers,
            )
            _raise_ebay(r3, "publish")
            listing_id = r3.json().get("listingId")

        return ListingResult(
            sku=sku,
            offer_id=offer_id,
            listing_id=listing_id,
            status="published",
            list_price=list_price,
            response={"offerId": offer_id, "listingId": listing_id},
            message=f"Published to eBay {'live' if self.live else 'sandbox'}.",
        )

    def create_set_listing(self, cards: list, list_price: float) -> ListingResult:
        """Combine multiple cards into ONE eBay lot listing (all cards + photos)."""
        s = get_settings()
        self._require_config(s)
        sku = set_sku(cards)
        image_urls = set_image_urls(cards, s.public_image_base_url)
        if self.live and not image_urls:
            raise MissingCredentialsError(
                "Live eBay listings require an image. Set PUBLIC_IMAGE_BASE_URL so "
                "the card crops are reachable by eBay."
            )

        product: dict = {"title": build_set_title(cards), "aspects": build_set_aspects(cards)}
        if image_urls:
            product["imageUrls"] = image_urls
        inventory_payload = {
            "product": product,
            "condition": map_condition(cards[0]),
            "conditionDescriptors": build_condition_descriptors(cards[0]),
            "availability": {"shipToLocationAvailability": {"quantity": 1}},
        }
        description = build_set_description(cards, shown_images=len(image_urls))

        headers = self._headers()
        with httpx.Client(base_url=self.api_base, timeout=60) as client:
            r1 = _send_with_retry(
                client.put,
                f"/sell/inventory/v1/inventory_item/{sku}",
                headers=headers,
                json=inventory_payload,
            )
            r1.raise_for_status()

            offer_id = self._get_or_create_offer(
                client, headers, s, sku, list_price,
                category_id=s.ebay_lot_category_id, description=description,
            )

            r3 = _send_with_retry(
                client.post,
                f"/sell/inventory/v1/offer/{offer_id}/publish",
                headers=headers,
            )
            r3.raise_for_status()
            listing_id = r3.json().get("listingId")

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

    def _get_or_create_offer(
        self, client, headers, s, sku, list_price, *, category_id=None, description=None
    ) -> str:
        """Reuse an existing offer for this SKU if present, else create one."""
        payload = self._offer_payload(
            s, sku, list_price, category_id=category_id, description=description
        )
        existing = client.get(
            "/sell/inventory/v1/offer", headers=headers, params={"sku": sku}
        )
        if existing.status_code == 200:
            offers = existing.json().get("offers", [])
            if offers:
                offer_id = offers[0]["offerId"]
                _send_with_retry(
                    client.put,
                    f"/sell/inventory/v1/offer/{offer_id}",
                    headers=headers,
                    json=payload,
                ).raise_for_status()
                return offer_id

        created = _send_with_retry(
            client.post, "/sell/inventory/v1/offer", headers=headers, json=payload,
        )
        created.raise_for_status()
        return created.json().get("offerId")

    @staticmethod
    def _offer_payload(s, sku, list_price, *, category_id=None, description=None) -> dict:
        auto_accept = round(list_price * 0.80, 2)
        payload = {
            "sku": sku,
            "marketplaceId": s.ebay_marketplace_id,
            "format": "FIXED_PRICE",
            "availableQuantity": 1,
            "categoryId": category_id or s.ebay_category_id,
            "listingPolicies": {
                "fulfillmentPolicyId": s.ebay_fulfillment_policy_id,
                "paymentPolicyId": s.ebay_payment_policy_id,
                "returnPolicyId": s.ebay_return_policy_id,
                "bestOfferTerms": {
                    "bestOfferEnabled": True,
                    "autoAcceptPrice": {
                        "value": str(auto_accept),
                        "currency": "USD",
                    },
                },
            },
            "merchantLocationKey": s.ebay_merchant_location_key,
            "pricingSummary": {"price": {"value": str(list_price), "currency": "USD"}},
        }
        if description:
            payload["listingDescription"] = description
        return payload
