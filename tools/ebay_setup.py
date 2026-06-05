"""One-time eBay seller setup: business policies + inventory location.

Run AFTER you have an EBAY_USER_REFRESH_TOKEN (see /ebay/oauth/start). Uses the
Sell Account + Inventory APIs to fetch-or-create the fulfillment (shipping),
payment, and return policies plus an inventory location, then writes the
resulting IDs into .env so live listings have everything they need.

Usage:
  python -m tools.ebay_setup policies
  python -m tools.ebay_setup location --postal 90210 --city "Los Angeles" \
      --state CA --country US
  python -m tools.ebay_setup all --postal 90210 --city "Los Angeles" \
      --state CA --country US

Idempotent: existing policies/locations are reused rather than duplicated.
"""
from __future__ import annotations

import argparse
import re
import sys

import httpx

from app.config import ROOT_DIR, get_settings
from app.services.ebay.oauth import get_user_access_token

LIVE_API = "https://api.ebay.com"
SANDBOX_API = "https://api.sandbox.ebay.com"
_ENV_PATH = ROOT_DIR / ".env"


def _write_env_value(key: str, value: str) -> None:
    text = _ENV_PATH.read_text(encoding="utf-8") if _ENV_PATH.exists() else ""
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    line = f"{key}={value}"
    text = pattern.sub(line, text) if pattern.search(text) else text.rstrip("\n") + f"\n{line}\n"
    _ENV_PATH.write_text(text, encoding="utf-8")
    print(f"  .env: {key}={value}")


def _ctx() -> tuple[str, str, dict]:
    s = get_settings()
    live = s.ebay_mode.lower() == "live"
    api = LIVE_API if live else SANDBOX_API
    token = get_user_access_token(live=live)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Content-Language": "en-US",
    }
    return api, s.ebay_marketplace_id, headers


# --- Business policies -------------------------------------------------------

_CATEGORY_TYPE = [{"name": "ALL_EXCLUDING_MOTORS_VEHICLES"}]


def _get_or_create(client, headers, kind: str, mkt: str, create_body: dict, id_field: str) -> str:
    """kind: fulfillment_policy | payment_policy | return_policy."""
    listing = client.get(
        f"/sell/account/v1/{kind}", headers=headers, params={"marketplace_id": mkt}
    )
    if listing.status_code == 200:
        # e.g. fulfillment_policy -> {"fulfillmentPolicies": [...]}
        items = listing.json().get(f"{kind.replace('_policy', '')}Policies", [])
        if items:
            pid = items[0][id_field]
            print(f"  {kind}: reusing existing {pid}")
            return pid
    created = client.post(f"/sell/account/v1/{kind}", headers=headers, json=create_body)
    if created.status_code not in (200, 201):
        print(f"  ✗ {kind} create failed {created.status_code}: {created.text}")
        created.raise_for_status()
    pid = created.json()[id_field]
    print(f"  {kind}: created {pid}")
    return pid


def setup_policies() -> None:
    api, mkt, headers = _ctx()
    print(f"Setting up business policies on {api} ({mkt})...")
    with httpx.Client(base_url=api, timeout=60) as client:
        ful = _get_or_create(
            client, headers, "fulfillment_policy", mkt,
            {
                "name": "Cards Standard Shipping",
                "marketplaceId": mkt,
                "categoryTypes": _CATEGORY_TYPE,
                "handlingTime": {"value": 3, "unit": "DAY"},
                "shippingOptions": [
                    {
                        "optionType": "DOMESTIC",
                        "costType": "FLAT_RATE",
                        "shippingServices": [
                            {
                                "sortOrder": 1,
                                "shippingServiceCode": "USPSGroundAdvantage",
                                "shippingCost": {"value": "5.00", "currency": "USD"},
                                "freeShipping": False,
                            }
                        ],
                    }
                ],
            },
            "fulfillmentPolicyId",
        )
        pay = _get_or_create(
            client, headers, "payment_policy", mkt,
            {
                "name": "Cards Payment",
                "marketplaceId": mkt,
                "categoryTypes": _CATEGORY_TYPE,
                # Managed payments: methods are handled by eBay; immediate pay on.
                "immediatePay": True,
            },
            "paymentPolicyId",
        )
        ret = _get_or_create(
            client, headers, "return_policy", mkt,
            {
                "name": "Cards Returns",
                "marketplaceId": mkt,
                "categoryTypes": _CATEGORY_TYPE,
                "returnsAccepted": True,
                "returnPeriod": {"value": 30, "unit": "DAY"},
                "returnShippingCostPayer": "SELLER",
                "returnMethod": "REPLACEMENT",
            },
            "returnPolicyId",
        )
    _write_env_value("EBAY_FULFILLMENT_POLICY_ID", ful)
    _write_env_value("EBAY_PAYMENT_POLICY_ID", pay)
    _write_env_value("EBAY_RETURN_POLICY_ID", ret)


# --- Inventory location ------------------------------------------------------


def setup_location(postal: str, city: str, state: str, country: str) -> None:
    api, mkt, headers = _ctx()
    key = "cards-default"
    print(f"Creating inventory location '{key}' on {api}...")
    body = {
        "location": {
            "address": {
                "city": city,
                "stateOrProvince": state,
                "postalCode": postal,
                "country": country,
            }
        },
        "locationInstructions": "Card inventory",
        "name": "Cards Default Location",
        "merchantLocationStatus": "ENABLED",
        "locationTypes": ["WAREHOUSE"],
    }
    with httpx.Client(base_url=api, timeout=60) as client:
        r = client.post(
            f"/sell/inventory/v1/location/{key}", headers=headers, json=body
        )
        if r.status_code == 204:
            print(f"  created {key}")
        elif r.status_code == 409:
            print(f"  {key}: already exists, reusing")
        else:
            print(f"  ✗ location failed {r.status_code}: {r.text}")
            r.raise_for_status()
    _write_env_value("EBAY_MERCHANT_LOCATION_KEY", key)


def main() -> None:
    p = argparse.ArgumentParser(description="eBay seller setup")
    p.add_argument("command", choices=["policies", "location", "all"])
    p.add_argument("--postal")
    p.add_argument("--city")
    p.add_argument("--state")
    p.add_argument("--country", default="US")
    args = p.parse_args()

    s = get_settings()
    if not s.ebay_user_refresh_token:
        sys.exit("EBAY_USER_REFRESH_TOKEN is not set — run /ebay/oauth/start first.")

    if args.command in ("policies", "all"):
        setup_policies()
    if args.command in ("location", "all"):
        if not all([args.postal, args.city, args.state]):
            sys.exit("location needs --postal --city --state (and --country, default US)")
        setup_location(args.postal, args.city, args.state, args.country)
    print("\nDone. Restart the app to load the new .env values.")


if __name__ == "__main__":
    main()
