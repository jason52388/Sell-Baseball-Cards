# Permanent eBay account-deletion endpoint (VPS, $0)

eBay requires a public HTTPS endpoint that answers a validation challenge and
acknowledges account-deletion notifications, and it **pings that endpoint 24/7**.
Hosting it via the laptop + ngrok means every laptop sleep or ngrok restart looks
like an outage — which is exactly the "endpoint down / non-responsive" email that
threatens key deactivation.

This kit moves *only* that endpoint onto your always-on VPS. It's a tiny,
stateless responder (stores no data) fronted by Caddy for free, auto-renewing
HTTPS. Cost: **$0** beyond the VPS you already have.

> Your card app keeps running on the laptop + ngrok. Listing **images** don't
> need 24/7 hosting — eBay copies images into its own picture service when a
> listing is created, so they only need to be reachable while you're actively
> listing (when the laptop is up anyway).

## What gets installed on the VPS

| Piece | Role |
|-------|------|
| `responder.py` → `/opt/ebay-deletion/` | zero-dependency HTTP responder (systemd service `ebay-deletion`, loopback :8787) |
| Caddy + `sites/ebay-deletion.caddy` | terminates TLS, auto Let's Encrypt cert, reverse-proxies to :8787 |
| `ebay-duckdns.timer` | refreshes the DuckDNS A record every 5 min |
| `/etc/ebay-deletion.env` | the verification token + endpoint URL |

## One-time setup

### 1. Get a free DuckDNS hostname
1. Go to <https://www.duckdns.org>, sign in (GitHub/Google), and create a
   subdomain, e.g. `myebaycards` → `myebaycards.duckdns.org`.
2. Copy your **token** from the top of the DuckDNS page.

### 2. Copy this folder to the VPS and run the installer
```bash
# from your laptop (repo root):
scp -r deploy/ebay-deletion user@YOUR_VPS:/tmp/ebay-deletion

# on the VPS:
cd /tmp/ebay-deletion
sudo bash install.sh \
  --subdomain     myebaycards \
  --duckdns-token XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX \
  --ebay-token    YOUR_EBAY_VERIFICATION_TOKEN
```
Reuse your **existing** `EBAY_VERIFICATION_TOKEN` (from the app's `.env`) so you
only change the URL in the eBay portal, not the token. The installer points
DuckDNS at the box, installs Caddy, waits for the certificate, and prints
`SUCCESS ✅` with the public endpoint URL once it verifies end-to-end.

> If the VPS is behind a cloud-provider firewall/security group, open inbound
> **TCP 80 and 443** there too (Caddy needs 80 for the certificate challenge).

### 3. Point eBay at the new endpoint
eBay Developer portal → your account → **Alerts & Notifications** →
**Marketplace account deletion**:
- **Notification endpoint URL:** `https://myebaycards.duckdns.org/ebay/account-deletion`
- **Verification token:** the same token you passed to `install.sh`
- Save, then click **Send Test Notification**.

eBay sends a fresh challenge to the new URL; the responder answers it and eBay
clears the "down" flag. (You have 30 days from the warning email.)

### 4. Confirm
```bash
# on the VPS:
sudo ./verify.sh          # -> OK: 200 ... challengeResponse hash matches
```

## Keep the app consistent (optional)
In the card app's `.env`, set `EBAY_DELETION_ENDPOINT_URL` to the same DuckDNS
URL so the app's built-in `/ebay/account-deletion` route computes an identical
hash (a warm spare if you ever point eBay back at it). Leave
`PUBLIC_IMAGE_BASE_URL` on the ngrok domain — images still serve from there.
Restart the app after editing `.env` (settings are cached at startup).

## Operating it
```bash
systemctl status ebay-deletion          # responder
journalctl -u ebay-deletion -f          # its logs (one line per eBay ping)
systemctl status caddy                  # TLS / proxy
journalctl -u caddy -n 50 --no-pager    # cert issuance / renewal
```
To change the token or URL: edit `/etc/ebay-deletion.env`, then
`sudo systemctl restart ebay-deletion` and re-run `verify.sh`.

## Not on a fresh box?
The installer aborts if something other than Caddy already owns ports 80/443.
If you run **nginx**, add a server block that proxies
`location /ebay/account-deletion { proxy_pass http://127.0.0.1:8787; }` on an
HTTPS vhost for your DuckDNS host (certbot for the cert), install just the
`ebay-deletion.service` + `/etc/ebay-deletion.env`, and skip Caddy. Same for a
Docker setup — run `responder.py` as a container on :8787 behind your existing
reverse proxy. Ask and I'll tailor it.
