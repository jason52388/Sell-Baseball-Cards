#!/usr/bin/env bash
# Self-healing guard for the eBay account-deletion route on the Hostinger VPS.
#
# Context: the edge reverse proxy is a Caddy CONTAINER (`warren-caddy`, part of
# the unrelated `warren-bot` project) that owns ports 80/443. Its Caddyfile is
# baked from that project's git repo, and its deploy.yml runs
# `git reset --hard origin/main` + `docker compose up -d --build` — so any edit
# we make to warren-caddy's config is wiped on the next warren-bot redeploy.
#
# Rather than commit an eBay route into an unrelated repo, this guard keeps the
# route alive out-of-band: every 60s (systemd timer) it checks whether
# warren-caddy is serving the route and re-applies it if missing. Idempotent;
# a no-op when healthy. Installed at /opt/ebay-deletion/ on the VPS.
set -euo pipefail
CADDY=warren-caddy
HOST=ebay.yalamanbaby.com
UPSTREAM=ebay-deletion:8787

docker inspect -f "{{.State.Running}}" "$CADDY" 2>/dev/null | grep -q true || exit 0
docker inspect -f "{{.State.Running}}" ebay-deletion 2>/dev/null | grep -q true || exit 0
if docker exec "$CADDY" grep -q "$HOST" /etc/caddy/Caddyfile 2>/dev/null; then exit 0; fi

docker exec -i "$CADDY" sh -c "cat >> /etc/caddy/Caddyfile" <<EOF

# eBay account-deletion route (re-applied by ebay-route-guard.timer)
$HOST {
    reverse_proxy $UPSTREAM
}
EOF
if docker exec "$CADDY" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
  docker exec "$CADDY" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 && \
    logger -t ebay-route-guard "re-applied eBay route to $CADDY"
else
  logger -t ebay-route-guard "validation FAILED after appending route; not reloading"
fi
