#!/usr/bin/env python3
"""Live-verify the 130point sold-comp source against the real site.

Run this where outbound network is allowed (it WON'T work inside the locked-down
web session — every host there returns 403 "Host not in allowlist"). It performs
one real search and prints what `parse_results_html` extracts, so you can confirm
the selectors still match 130point's current markup.

Usage:
    python -m tools.verify_130point "1989 Upper Deck Ken Griffey Jr #1"
    python -m tools.verify_130point "..." --raw   # dump raw HTML to inspect markup

If it prints 0 comps but the site shows results in a browser, the markup has
changed: dump the raw HTML (--raw) and adjust the selectors in
app/services/point130.py:parse_results_html.
"""
from __future__ import annotations

import argparse
import sys

import httpx

from app.services import point130


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", default="1989 Upper Deck Ken Griffey Jr #1")
    ap.add_argument("--raw", action="store_true", help="print raw HTML and exit")
    args = ap.parse_args()

    try:
        resp = httpx.post(
            point130._SEARCH_URL,
            data=point130.build_search_payload(args.query),
            headers={
                "User-Agent": point130._UA,
                "Accept-Language": "en-US,en;q=0.9",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": point130._SITE,
            },
            timeout=30,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"request failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(f"HTTP {resp.status_code}, {len(resp.text)} bytes")
    if args.raw:
        print(resp.text)
        return 0

    comps = point130.parse_results_html(resp.text)
    print(f"parsed {len(comps)} comps for {args.query!r}\n")
    for c in comps[:25]:
        print(f"  ${c.sold_price:<8} {c.sold_date or '?':<12} "
              f"[{c.source}] {c.title[:70]}")
    if not comps:
        print("  (none — re-run with --raw and check the selectors)")
    return 0 if comps else 1


if __name__ == "__main__":
    raise SystemExit(main())
