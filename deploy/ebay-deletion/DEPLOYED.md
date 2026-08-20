# Production deployment record — eBay account-deletion endpoint

**Live URL:** `https://ebay.yalamanbaby.com/ebay/account-deletion`
**Deployed:** 2026-06-30, on the Hostinger VPS `srv1636359.hstgr.cloud` (`177.7.58.66`).
**Cost:** $0 (reuses the existing VPS + Caddy + Let's Encrypt).

## Why this layout (not the generic install.sh)
The VPS is **not** a fresh box. Ports 80/443 are owned by a Caddy **container**
(`warren-caddy`, from the unrelated `warren-bot` project) that already serves
`yalamanstockmarket.com` / `yalamanbaby.com` and is explicitly set up to
reverse-proxy other apps on the shared `proxy-shared` Docker network. So the eBay
responder plugs into that instead of installing a second web server.

DuckDNS was tried first and **abandoned**: Let's Encrypt could not reliably
resolve `*.duckdns.org` (secondary-validation DNS timeouts), which would also
break the 60-day renewal. `ebay.yalamanbaby.com` is on Hostinger DNS, which
issues/renews certs reliably on this box.

## Moving parts
| Where | What |
|-------|------|
| `/root/ebay-deletion/` | this kit's `docker-compose.yml` + `Dockerfile` + `responder.py` + `.env`. Container `ebay-deletion` (restart=unless-stopped) on `proxy-shared`, no host ports. |
| `warren-caddy` | routes `ebay.yalamanbaby.com` → `ebay-deletion:8787`, terminates TLS (auto Let's Encrypt). |
| `/opt/ebay-deletion/ensure-caddy-route.sh` + `ebay-route-guard.{service,timer}` | **self-healing guard.** warren-bot's `deploy.yml` does `git reset --hard` + rebuild, which would wipe the Caddy route; the timer re-applies it within 60s. Verified by simulating a wipe. |
| DNS | `ebay.yalamanbaby.com` A → `177.7.58.66` (Hostinger hPanel, added manually). |

## Redeploy / recover
```bash
# responder
cd /root/ebay-deletion && docker compose up -d --build
# route (normally automatic via the guard)
systemctl start ebay-route-guard.service
# health (from anywhere)
curl "https://ebay.yalamanbaby.com/ebay/account-deletion?challenge_code=test"   # -> {"challengeResponse": "..."}
```
`verify.sh` (with `EBAY_DELETION_ENDPOINT_URL`/`EBAY_VERIFICATION_TOKEN` set) does
the full hash check.

## eBay portal
Endpoint URL = the live URL above; verification token = the app's
`EBAY_VERIFICATION_TOKEN`. After changing, click **Send Test Notification** to
clear any "down" flag.

> The generic `install.sh` / `ebay-deletion.service` / `Caddyfile` in the parent
> folder remain valid for a **fresh** box (host Caddy + DuckDNS). They are not
> what's used here.

## The two URLs are independent — don't collapse them
Since this deployment, the card app's `.env` carries two *unrelated* public URLs:

| Setting | Points at | Used by | ngrok involved? |
|---|---|---|---|
| `EBAY_DELETION_ENDPOINT_URL` | `https://ebay.yalamanbaby.com/ebay/account-deletion` (this VPS) | `tools/verify_deletion_endpoint.sh`, and the app's own spare `/ebay/account-deletion` route | **No** — always up |
| `PUBLIC_IMAGE_BASE_URL` | the reserved ngrok domain | `tools/ebay_tunnel.sh`, and the `imageUrls` sent to eBay when publishing | **Yes** — only while publishing |

`ebay_tunnel.sh` used to derive its ngrok domain from `EBAY_DELETION_ENDPOINT_URL`,
which was left over from when both lived on ngrok. That made correcting the
deletion URL to this VPS silently break listing images. It now reads
`PUBLIC_IMAGE_BASE_URL`, which is what it actually serves.

**The tunnel is only needed while a listing is being published.** eBay downloads
each crop and re-hosts it on `i.ebayimg.com` during publish, so once a listing is
live the tunnel can be stopped and the photos keep working. Verified against live
listings `306985703638` and `306985707317` with the tunnel offline: both still
serve their photos from eBay's CDN.
