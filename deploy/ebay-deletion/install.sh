#!/usr/bin/env bash
# One-command installer for the always-on eBay account-deletion endpoint.
#
# Run this ON THE VPS as root (or with sudo). It sets up, at $0 cost:
#   - a free DuckDNS hostname pointed at this box (auto-refreshed)
#   - Caddy, which fetches + auto-renews a free Let's Encrypt certificate
#   - a tiny always-on systemd service (responder.py) that answers eBay
#
# Usage (interactive prompts if flags omitted):
#   sudo ./install.sh \
#       --subdomain YOURSUB \
#       --duckdns-token XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX \
#       --ebay-token   YOUR_EBAY_VERIFICATION_TOKEN
#
# Idempotent: safe to re-run to update config.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SUBDOMAIN=""; DUCKDNS_TOKEN=""; EBAY_TOKEN=""

while [ $# -gt 0 ]; do
  case "$1" in
    --subdomain)     SUBDOMAIN="$2"; shift 2 ;;
    --duckdns-token) DUCKDNS_TOKEN="$2"; shift 2 ;;
    --ebay-token)    EBAY_TOKEN="$2"; shift 2 ;;
    -h|--help)       sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

die() { echo "ERROR: $*" >&2; exit 1; }
[ "$(id -u)" = "0" ] || die "run as root (sudo ./install.sh ...)"
command -v apt-get >/dev/null 2>&1 || die "this installer targets Debian/Ubuntu (apt). For another distro, follow README.md manually."

prompt() { local v; read -r -p "$1" v; printf '%s' "$v"; }
[ -n "$SUBDOMAIN" ]     || SUBDOMAIN="$(prompt 'DuckDNS subdomain (the part before .duckdns.org): ')"
[ -n "$DUCKDNS_TOKEN" ] || DUCKDNS_TOKEN="$(prompt 'DuckDNS token: ')"
[ -n "$EBAY_TOKEN" ]    || EBAY_TOKEN="$(prompt 'eBay verification token (reuse existing if you have one): ')"

SUBDOMAIN="${SUBDOMAIN%.duckdns.org}"   # tolerate a full hostname being pasted
[ -n "$SUBDOMAIN" ] && [ -n "$DUCKDNS_TOKEN" ] && [ -n "$EBAY_TOKEN" ] || die "subdomain, duckdns token and ebay token are all required"
printf '%s' "$EBAY_TOKEN" | grep -qE '^[A-Za-z0-9_-]{32,80}$' || die "eBay verification token must be 32-80 chars of [A-Za-z0-9_-]"

FQDN="${SUBDOMAIN}.duckdns.org"
ENDPOINT_URL="https://${FQDN}/ebay/account-deletion"
echo "==> Target endpoint: ${ENDPOINT_URL}"

# --- Preflight: make sure nothing else owns 80/443 (they said 'not sure') -----
BUSY="$(ss -ltnH 'sport = :80 or sport = :443' 2>/dev/null | awk '{print $4}' || true)"
if [ -n "$BUSY" ]; then
  if ! ss -ltnpH 'sport = :80 or sport = :443' 2>/dev/null | grep -q 'caddy'; then
    echo "WARNING: something is already listening on 80/443:" >&2
    ss -ltnpH 'sport = :80 or sport = :443' 2>/dev/null >&2 || true
    echo "         If that's nginx/apache, Caddy will conflict. Stop it, or use" >&2
    echo "         the nginx path in README.md. Aborting to avoid breaking it." >&2
    exit 1
  fi
fi

export DEBIAN_FRONTEND=noninteractive

# --- Point DuckDNS at this box ------------------------------------------------
echo "==> Updating DuckDNS A record for ${FQDN} ..."
RESP="$(curl -fsS "https://www.duckdns.org/update?domains=${SUBDOMAIN}&token=${DUCKDNS_TOKEN}&ip=" || true)"
[ "$RESP" = "OK" ] || die "DuckDNS update returned '${RESP}' (expected OK). Check the subdomain + token."

# --- Install Caddy (official repo) + python3 ----------------------------------
if ! command -v caddy >/dev/null 2>&1; then
  echo "==> Installing Caddy ..."
  apt-get update -qq
  apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gnupg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy
fi
command -v python3 >/dev/null 2>&1 || apt-get install -y -qq python3

# --- Service user + responder -------------------------------------------------
id -u ebaydel >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin ebaydel
install -d -m 0755 /opt/ebay-deletion
install -m 0644 "${SCRIPT_DIR}/responder.py" /opt/ebay-deletion/responder.py

umask 077
cat > /etc/ebay-deletion.env <<EOF
EBAY_VERIFICATION_TOKEN=${EBAY_TOKEN}
EBAY_DELETION_ENDPOINT_URL=${ENDPOINT_URL}
BIND=127.0.0.1
PORT=8787
EOF
chown root:ebaydel /etc/ebay-deletion.env
chmod 0640 /etc/ebay-deletion.env

install -m 0644 "${SCRIPT_DIR}/ebay-deletion.service" /etc/systemd/system/ebay-deletion.service

# --- DuckDNS auto-refresh (systemd timer, every 5 min) ------------------------
cat > /etc/duckdns.env <<EOF
DUCKDNS_SUBDOMAIN=${SUBDOMAIN}
DUCKDNS_TOKEN=${DUCKDNS_TOKEN}
EOF
chmod 0600 /etc/duckdns.env
cat > /opt/ebay-deletion/duckdns-update.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
. /etc/duckdns.env
curl -fsS "https://www.duckdns.org/update?domains=${DUCKDNS_SUBDOMAIN}&token=${DUCKDNS_TOKEN}&ip=" >/dev/null
EOF
chmod 0700 /opt/ebay-deletion/duckdns-update.sh
cat > /etc/systemd/system/ebay-duckdns.service <<'EOF'
[Unit]
Description=Refresh DuckDNS A record for the eBay deletion endpoint
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/opt/ebay-deletion/duckdns-update.sh
EOF
cat > /etc/systemd/system/ebay-duckdns.timer <<'EOF'
[Unit]
Description=Refresh DuckDNS every 5 minutes
[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
Persistent=true
[Install]
WantedBy=timers.target
EOF

# --- Caddy site (non-destructive: import a dedicated file) --------------------
install -d -m 0755 /etc/caddy/sites
cat > /etc/caddy/sites/ebay-deletion.caddy <<EOF
${FQDN} {
    encode gzip
    reverse_proxy 127.0.0.1:8787
}
EOF
if ! grep -q 'import /etc/caddy/sites/\*.caddy' /etc/caddy/Caddyfile 2>/dev/null; then
  # Back up the stock Caddyfile once, then replace with a minimal importer so the
  # default :80 sample block can't shadow our site.
  [ -f /etc/caddy/Caddyfile ] && cp -n /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.bak.$(date +%s)" || true
  cat > /etc/caddy/Caddyfile <<'EOF'
# Managed by deploy/ebay-deletion/install.sh — site configs live in sites/*.caddy
import /etc/caddy/sites/*.caddy
EOF
fi

# --- Firewall (best effort) ---------------------------------------------------
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow 80/tcp  >/dev/null 2>&1 || true
  ufw allow 443/tcp >/dev/null 2>&1 || true
fi

# --- Start everything ---------------------------------------------------------
echo "==> Starting services ..."
systemctl daemon-reload
systemctl enable --now ebay-deletion.service >/dev/null
systemctl enable --now ebay-duckdns.timer >/dev/null
caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1 || die "Caddy config invalid"
systemctl reload caddy 2>/dev/null || systemctl restart caddy
systemctl enable caddy >/dev/null 2>&1 || true

# --- Wait for DNS + cert, then verify -----------------------------------------
echo "==> Waiting for ${FQDN} to resolve to this box ..."
MYIP="$(curl -fsS https://api.ipify.org || true)"
for _ in $(seq 1 30); do
  R="$(getent hosts "$FQDN" | awk '{print $1}' | head -1 || true)"
  [ -n "$R" ] && { echo "    ${FQDN} -> ${R} (this box: ${MYIP:-?})"; break; }
  sleep 2
done

echo "==> Waiting for HTTPS certificate (Caddy/Let's Encrypt, up to ~60s) ..."
OK=""
for _ in $(seq 1 30); do
  CODE="probe_$(date +%s)"
  BODY="$(curl -fsS -m 8 "https://${FQDN}/ebay/account-deletion?challenge_code=${CODE}" 2>/dev/null || true)"
  if printf '%s' "$BODY" | grep -q 'challengeResponse'; then OK=1; break; fi
  sleep 2
done

echo
if [ -n "$OK" ]; then
  echo "SUCCESS ✅  Public endpoint is live: ${ENDPOINT_URL}"
  echo
  echo "Next: in the eBay Developer portal set the marketplace account-deletion"
  echo "endpoint URL to exactly:"
  echo "    ${ENDPOINT_URL}"
  echo "and the verification token to the one you used here, then click"
  echo "'Send Test Notification'. Confirm anytime with:  ./verify.sh"
else
  echo "Services are up but the public HTTPS check didn't pass yet."
  echo "Common causes: cloud-provider firewall still blocking 80/443, or DNS not"
  echo "propagated. Check:  journalctl -u caddy -n 50 --no-pager  and  ./verify.sh"
fi
