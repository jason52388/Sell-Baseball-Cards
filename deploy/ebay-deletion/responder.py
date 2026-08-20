#!/usr/bin/env python3
"""Standalone eBay Marketplace Account Deletion / Closure endpoint responder.

Zero dependencies (Python standard library only). Runs as an always-on systemd
service on the VPS so eBay's 24/7 validation pings never depend on the laptop or
the ngrok tunnel being up. It stores no data — it only answers the validation
challenge and acknowledges deletion notifications.

Behaviour mirrors app/routers/ebay_notifications.py exactly:

  GET  /ebay/account-deletion?challenge_code=CODE
       -> 200 application/json {"challengeResponse": sha256(CODE + TOKEN + URL)}
  POST /ebay/account-deletion
       -> 200 (we persist no eBay user data; nothing to erase)
  GET  /healthz
       -> 200 "ok"

Configuration comes from the environment (see ebay-deletion.env.example):

  EBAY_VERIFICATION_TOKEN     required. Any 32-80 char [A-Za-z0-9_-] string that
                              is ALSO pasted into the eBay portal.
  EBAY_DELETION_ENDPOINT_URL  required. Must EXACTLY match the public HTTPS URL
                              registered in the eBay portal — it is part of the
                              challenge hash. e.g.
                              https://yoursub.duckdns.org/ebay/account-deletion
  BIND                        interface to listen on (default 127.0.0.1; Caddy
                              terminates TLS and reverse-proxies to us).
  PORT                        port to listen on (default 8787).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PATH = "/ebay/account-deletion"
TOKEN = os.environ.get("EBAY_VERIFICATION_TOKEN", "")
ENDPOINT = os.environ.get("EBAY_DELETION_ENDPOINT_URL", "")
BIND = os.environ.get("BIND", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8787"))
# eBay's notification bodies are a few KB; anything beyond this is not ours.
_MAX_BODY_BYTES = 1024 * 1024

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("ebay-deletion")


class Handler(BaseHTTPRequestHandler):
    server_version = "ebay-deletion/1.0"
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: dict) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    def _text(self, status: int, text: str) -> None:
        self._send(status, text.encode(), "text/plain")

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        u = urlparse(self.path)
        if u.path == "/healthz":
            return self._text(200, "ok")
        if u.path != PATH:
            return self._text(404, "not found")
        if not TOKEN or not ENDPOINT:
            log.error(
                "challenge received but EBAY_VERIFICATION_TOKEN / "
                "EBAY_DELETION_ENDPOINT_URL are not configured"
            )
            return self._json(500, {"error": "endpoint not configured"})
        codes = parse_qs(u.query).get("challenge_code")
        if not codes:
            return self._json(400, {"error": "missing challenge_code"})
        h = hashlib.sha256()
        h.update(codes[0].encode("utf-8"))
        h.update(TOKEN.encode("utf-8"))
        h.update(ENDPOINT.encode("utf-8"))
        log.info("challenge answered")
        return self._json(200, {"challengeResponse": h.hexdigest()})

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        u = urlparse(self.path)
        if u.path != PATH:
            return self._text(404, "not found")
        # Drain any body (eBay sends JSON). We store nothing; just acknowledge.
        # Capped: this endpoint is public, and reading an attacker-declared
        # Content-Length in full would let one request balloon memory.
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length > 0:
                self.rfile.read(min(length, _MAX_BODY_BYTES))
        except (ValueError, OSError):
            pass
        log.info("account-deletion notification acknowledged")
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:  # quieter, structured
        log.info("%s %s", self.address_string(), fmt % args)


def main() -> None:
    if not TOKEN or not ENDPOINT:
        log.warning(
            "starting WITHOUT full config; the GET challenge will return 500 "
            "until EBAY_VERIFICATION_TOKEN and EBAY_DELETION_ENDPOINT_URL are set"
        )
    log.info(
        "eBay deletion responder listening on %s:%s (endpoint=%s)",
        BIND,
        PORT,
        ENDPOINT or "<unset>",
    )
    # Without a timeout a slow or stalled client pins a worker thread forever.
    Handler.timeout = 30
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
