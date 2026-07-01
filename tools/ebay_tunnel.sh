#!/usr/bin/env bash
# Start the ngrok tunnel bound to the RESERVED domain that eBay has registered.
#
# WHY THIS EXISTS: `ngrok http 8000` picks a *random* URL, which does NOT match
# the domain eBay stored for the marketplace account-deletion endpoint and image
# serving. When that happens the reserved domain shows ERR_NGROK_3200 "offline"
# and eBay marks the app "down and non-responsive". This script always binds the
# exact host from .env so the public URL matches what eBay expects.
#
# Usage:  tools/ebay_tunnel.sh        # foreground (Ctrl-C to stop)
#
# The app itself must be running on :8000 (see run.sh). Verify afterwards with
# tools/verify_deletion_endpoint.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "ERROR: .env not found in $(pwd)" >&2
  exit 1
fi

# Single source of truth: the host eBay registered for the deletion endpoint.
url=$(grep -E '^EBAY_DELETION_ENDPOINT_URL=' .env | cut -d= -f2- | tr -d '"')
host=$(printf '%s' "$url" | sed -E 's#^https?://##; s#/.*$##')
if [ -z "$host" ]; then
  echo "ERROR: could not derive host from EBAY_DELETION_ENDPOINT_URL in .env" >&2
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
