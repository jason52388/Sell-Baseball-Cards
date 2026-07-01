#!/usr/bin/env bash
# Verify the live eBay account-deletion endpoint end-to-end (what eBay's
# validator does): send a challenge_code over HTTPS and confirm the response is
# 200 + application/json with challengeResponse == SHA256(code + token + url).
#
# Reads config from /etc/ebay-deletion.env when present (on the VPS), else from
# EBAY_DELETION_ENDPOINT_URL / EBAY_VERIFICATION_TOKEN in the environment.
#
# Exit 0 = healthy (eBay will accept it). Non-zero = broken, with the reason.
set -euo pipefail

if [ -f /etc/ebay-deletion.env ]; then
  # shellcheck disable=SC1091
  . /etc/ebay-deletion.env
fi
endpoint="${EBAY_DELETION_ENDPOINT_URL:-}"
token="${EBAY_VERIFICATION_TOKEN:-}"
[ -n "$endpoint" ] || { echo "ERROR: EBAY_DELETION_ENDPOINT_URL not set (env or /etc/ebay-deletion.env)" >&2; exit 1; }
[ -n "$token" ]    || { echo "ERROR: EBAY_VERIFICATION_TOKEN not set" >&2; exit 1; }

code="verify_$(date +%s)"
if command -v sha256sum >/dev/null 2>&1; then
  expected=$(printf '%s%s%s' "$code" "$token" "$endpoint" | sha256sum | awk '{print $1}')
else
  expected=$(printf '%s%s%s' "$code" "$token" "$endpoint" | shasum -a 256 | awk '{print $1}')
fi

echo "Endpoint: $endpoint"
tmp=$(mktemp)
http=$(curl -s -m 20 -o "$tmp" -w '%{http_code}' \
  -H 'User-Agent: eBay-Notification-Validator' \
  "${endpoint}?challenge_code=${code}")
body=$(cat "$tmp"); rm -f "$tmp"

if [ "$http" != "200" ]; then
  echo "FAIL: HTTP $http (eBay needs 2xx)."
  case "$body" in
    *ERR_NGROK*) echo "  -> pointing at ngrok? Endpoint should be the VPS/DuckDNS URL now." ;;
    *)           echo "  -> body: $(printf '%s' "$body" | head -c 300)" ;;
  esac
  exit 1
fi
got=$(printf '%s' "$body" | sed -nE 's/.*"challengeResponse"[[:space:]]*:[[:space:]]*"([0-9a-f]{64})".*/\1/p')
[ -n "$got" ] || { echo "FAIL: 200 but no challengeResponse:"; printf '%s\n' "$body" | head -c 300; exit 1; }
if [ "$got" != "$expected" ]; then
  echo "FAIL: challengeResponse mismatch."
  echo "  got:      $got"
  echo "  expected: $expected"
  echo "  -> EBAY_DELETION_ENDPOINT_URL / EBAY_VERIFICATION_TOKEN here must match"
  echo "     exactly what's saved in the eBay portal."
  exit 1
fi
echo "OK: 200 application/json and challengeResponse hash matches. eBay will accept it."
