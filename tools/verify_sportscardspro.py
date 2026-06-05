#!/usr/bin/env python3
"""Live-verify the SportsCardsPro individual-sales scrape against the real site.

The SportsCardsPro/PriceCharting API returns only AGGREGATE prices. Individual
sales live on the product web page's recent-sales table, which we scrape. This
tool runs the real two-step product lookup, then fetches and parses that page so
you can confirm the URL shape and the table selectors still match.

Needs a PRICECHARTING_TOKEN and outbound network (won't run inside a locked-down
web session — every host there returns 403 "Host not in allowlist").

Usage:
    python -m tools.verify_sportscardspro "1989 Upper Deck Ken Griffey Jr #1"
    python -m tools.verify_sportscardspro "..." --raw   # dump the page HTML
"""
from __future__ import annotations

import argparse
import sys

import httpx

from app.services import pricecharting as pc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", default="1989 Upper Deck Ken Griffey Jr #1")
    ap.add_argument("--raw", action="store_true", help="print raw page HTML and exit")
    args = ap.parse_args()

    if not pc.has_token():
        print("PRICECHARTING_TOKEN not set — required for the product lookup.",
              file=sys.stderr)
        return 2

    detail = pc._lookup_detail(args.query)
    if detail is None:
        print(f"no confident product match for {args.query!r}", file=sys.stderr)
        return 1

    print("Aggregate tiers from the API:")
    for c in pc.parse_pricecharting_json(detail) + pc.parse_grade_tiers(detail):
        print(f"  {c.condition_grade:<10} ${c.sold_price}")

    url = pc.product_page_url(detail)
    print(f"\nProduct page: {url}")
    if not url:
        return 1
    try:
        resp = httpx.get(url, headers={"User-Agent": pc._UA}, timeout=30,
                         follow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"page fetch failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.raw:
        print(resp.text)
        return 0

    sales = pc.parse_sales_table_html(resp.text, page_url=url)
    print(f"\nparsed {len(sales)} individual sales:")
    for c in sales[:30]:
        print(f"  ${c.sold_price:<8} {c.sold_date or '?':<12} "
              f"{(c.condition_grade or 'raw'):<8} {c.title[:60]}")
    if not sales:
        print("  (none — re-run with --raw and adjust parse_sales_table_html)")
    return 0 if sales else 1


if __name__ == "__main__":
    raise SystemExit(main())
