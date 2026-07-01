#!/usr/bin/env bash
# End-to-end check of the eBay Marketplace Account Deletion endpoint over the
# PUBLIC url (exactly what eBay's validator does): send a challenge_code, then
# confirm the response is 200 + application/json and that challengeResponse
# equals SHA256(challengeCode + verificationToken + endpointURL).
#
# Run this after (re)starting the app + tunnel, and before/after re-triggering
# validation in the eBay portal, to confirm eBay will see a healthy endpoint.
#
# Exit 0 = healthy (eBay will accept it). Non-zero = broken, with the reason.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo "ERROR: .env not found in $(pwd)" >&2; exit 1; }

endpoint=$(grep -E '^EBAY_DELETION_ENDPOINT_URL=' .env | cut -d= -f2- | tr -d '"')
token=$(grep -E '^EBAY_VERIFICATION_TOKEN=' .env | cut -d= -f2- | tr -d '"')
[ -n "$endpoint" ] || { echo "ERROR: EBAY_DELETION_ENDPOINT_URL unset in .env" >&2; exit 1; }
[ -n "$token" ]    || { echo "ERROR: EBAY_VERIFICATION_TOKEN unset in .env" >&2; exit 1; }

code="verify_$(date +%s)"
expected=$(printf '%s%s%s' "$code" "$token" "$endpoint" | shasum -a 256 | awk '{print $1}')

echo "Endpoint: $endpoint"
echo "Probing challenge (code=$code) ..."
tmp=$(mktemp)
http=$(curl -s -m 20 -o "$tmp" -w '%{http_code}' \
  -H 'User-Agent: eBay-Notification-Validator' \
  "${endpoint}?challenge_code=${code}")
body=$(cat "$tmp"); rm -f "$tmp"

if [ "$http" != "200" ]; then
  echo "FAIL: HTTP $http (eBay needs 2xx)."
  case "$body" in
    *ERR_NGROK_3200*|*offline*) echo "  -> ngrok tunnel is OFFLINE. Start it: tools/ebay_tunnel.sh" ;;
    *ERR_NGROK*)                echo "  -> ngrok error page returned; check the tunnel." ;;
    *)                          echo "  -> body: $(printf '%s' "$body" | head -c 300)" ;;
  esac
  exit 1
fi

got=$(printf '%s' "$body" | sed -nE 's/.*"challengeResponse"[[:space:]]*:[[:space:]]*"([0-9a-f]{64})".*/\1/p')
if [ -z "$got" ]; then
  echo "FAIL: 200 but no valid challengeResponse in body:"
  printf '%s\n' "$body" | head -c 300
  exit 1
fi
if [ "$got" != "$expected" ]; then
  echo "FAIL: challengeResponse mismatch."
  echo "  got:      $got"
  echo "  expected: $expected"
  echo "  -> Usually EBAY_DELETION_ENDPOINT_URL / EBAY_VERIFICATION_TOKEN in .env"
  echo "     don't match what's saved in the eBay portal (or app needs restart)."
  exit 1
fi

echo "OK: 200 application/json and challengeResponse hash matches."
echo "eBay will see a healthy endpoint. If it was flagged 'down', re-trigger"
echo "validation in the eBay portal (Alerts & Notifications -> Send Test Notification)."
