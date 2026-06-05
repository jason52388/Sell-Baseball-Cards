"""eBay Marketplace Account Deletion / Closure notification endpoint.

eBay requires every PRODUCTION keyset to register a public HTTPS endpoint that
(1) answers a one-time validation challenge and (2) accepts account-deletion
notifications. Until this is satisfied, eBay will not activate production keys.

Spec: https://developer.ebay.com/marketplace-account-deletion

Validation (GET):
    eBay calls  GET <endpoint>?challenge_code=<code>
    We must return 200 with JSON {"challengeResponse": <hash>} where
        hash = SHA256(challengeCode + verificationToken + endpointURL)  (hex)
    The endpointURL must EXACTLY match the URL registered in the eBay portal.

Notification (POST):
    eBay POSTs a JSON body when a user requests account closure/deletion.
    We acknowledge with 200. (We store no eBay user data, so there is nothing
    to delete — we simply log and accept.)
"""
from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ebay", tags=["ebay"])

# Path component only; the full public URL is configured via
# EBAY_DELETION_ENDPOINT_URL so the validation hash matches exactly.
_PATH = "/account-deletion"


@router.get(_PATH)
def verify_challenge(challenge_code: str) -> Response:
    """Answer eBay's validation challenge with the required SHA-256 hash."""
    s = get_settings()
    token = s.ebay_verification_token
    endpoint = s.ebay_deletion_endpoint_url
    if not token or not endpoint:
        logger.error(
            "eBay challenge received but EBAY_VERIFICATION_TOKEN / "
            "EBAY_DELETION_ENDPOINT_URL are not configured."
        )
        return JSONResponse(
            {"error": "verification endpoint not configured"}, status_code=500
        )

    h = hashlib.sha256()
    h.update(challenge_code.encode("utf-8"))
    h.update(token.encode("utf-8"))
    h.update(endpoint.encode("utf-8"))
    challenge_response = h.hexdigest()

    # Must be application/json with exactly this shape.
    return JSONResponse({"challengeResponse": challenge_response})


@router.post(_PATH)
async def receive_notification(request: Request) -> Response:
    """Acknowledge an account-deletion notification.

    We do not persist any eBay user data, so there is nothing to erase. We log
    the notification id (if present) for auditability and return 200.
    """
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - eBay may send keep-alive/empty bodies
        payload = None

    notif_id = None
    if isinstance(payload, dict):
        notif_id = (payload.get("notification") or {}).get("notificationId")
    logger.info("eBay account-deletion notification received (id=%s)", notif_id)

    # 200/204 both acknowledge; eBay expects a 2xx.
    return Response(status_code=200)
