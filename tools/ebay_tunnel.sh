#!/usr/bin/env bash
# Start the ngrok tunnel bound to the RESERVED domain that serves card images to
# eBay while a listing is being published.
#
# WHY THIS EXISTS: `ngrok http 8000` picks a *random* URL, which does NOT match
# PUBLIC_IMAGE_BASE_URL. eBay fetches each crop from that base URL at publish
# time, so a mismatched (or offline) tunnel makes a live listing fail with a
# missing-image error.
#
# WHAT THIS IS *NOT*: the account-deletion endpoint. That moved to the VPS
# permanently (see deploy/ebay-deletion/DEPLOYED.md) and does not involve ngrok
# any more, which is why this reads PUBLIC_IMAGE_BASE_URL and not
# EBAY_DELETION_ENDPOINT_URL. Deriving the image host from the deletion setting
# meant that correcting the deletion URL silently broke listing images.
#
# WHEN TO RUN IT: only while publishing listings. eBay downloads and re-hosts the
# photos on its own CDN during publish, so once a listing is live the tunnel can
# be stopped and the listing keeps its images.
#
# Usage:  tools/ebay_tunnel.sh        # foreground (Ctrl-C to stop)
#
# The app itself must be running on :8000 (see run.sh).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "ERROR: .env not found in $(pwd)" >&2
  exit 1
fi

# Single source of truth: the base URL eBay fetches card crops from.
# `|| true` because under `set -e` a non-matching grep would abort the script
# before the explanatory error below could run.
url=$(grep -E '^PUBLIC_IMAGE_BASE_URL=' .env | cut -d= -f2- | tr -d '"' || true)
host=$(printf '%s' "$url" | sed -E 's#^https?://##; s#/.*$##')
if [ -z "$host" ]; then
  echo "ERROR: PUBLIC_IMAGE_BASE_URL is unset or has no host in .env." >&2
  echo "       Set it to your reserved ngrok domain, e.g." >&2
  echo "       PUBLIC_IMAGE_BASE_URL=https://your-name.ngrok-free.dev" >&2
  exit 1
fi

# Warn (don't block) if the local app isn't up yet — the tunnel is useless
# without it, and eBay's challenge would get a 502.
if ! curl -sf -o /dev/null "http://127.0.0.1:8000/docs" 2>/dev/null; then
  echo "WARNING: local app not responding on http://127.0.0.1:8000 — start it first (./run.sh)." >&2
fi

echo "Binding ngrok to reserved domain: https://${host} -> http://localhost:8000"
# ngrok 3.x: --url pins the reserved domain (older alias: --domain).
exec ngrok http "--url=https://${host}" 8000
