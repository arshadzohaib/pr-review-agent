"""GitHub HMAC-SHA256 signature verification.

Verifies the 'X-Hub-Signature-256' header on incoming webhooks against GITHUB_WEBHOOK_SECRET.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Final

from fastapi import Header, HTTPException, Request, status

from app.core.config import get_settings

SIGNATURE_HEADER: Final[str] = "X-Hub-Signature-256"


def compute_signature(payload_bytes: bytes, secret: str) -> str:
    """Compute sha256=... signature for raw bytes using HMAC-SHA256."""
    digest = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(
    payload_bytes: bytes, signature_header: str | None, secret: str
) -> bool:
    """Validate signature using constant-time comparison."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = compute_signature(payload_bytes, secret)
    return hmac.compare_digest(expected, signature_header)


async def verify_github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(None, alias="X-Hub-Signature-256"),
) -> bytes:
    """FastAPI dependency to verify webhook signature and return raw body bytes."""
    settings = getattr(request.app.state, "settings", None) or get_settings()
    secret = settings.github_webhook_secret.get_secret_value()

    body = await request.body()
    if not verify_signature(body, x_hub_signature_256, secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Hub-Signature-256 signature",
        )
    return body
