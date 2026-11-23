"""Unit tests for app.api.security."""

from __future__ import annotations

from app.api.security import compute_signature, verify_signature

SECRET = "super_secret_webhook_key_12345"


def test_compute_and_verify_signature_valid() -> None:
    payload = b'{"action": "opened", "number": 1}'
    sig = compute_signature(payload, SECRET)

    assert sig.startswith("sha256=")
    assert verify_signature(payload, sig, SECRET) is True


def test_verify_signature_tampered_payload() -> None:
    payload = b'{"action": "opened", "number": 1}'
    sig = compute_signature(payload, SECRET)

    tampered = b'{"action": "opened", "number": 2}'
    assert verify_signature(tampered, sig, SECRET) is False


def test_verify_signature_wrong_secret() -> None:
    payload = b'{"action": "opened", "number": 1}'
    sig = compute_signature(payload, SECRET)

    assert verify_signature(payload, sig, "wrong_secret_12345") is False


def test_verify_signature_malformed_header() -> None:
    payload = b'{"action": "opened"}'

    assert verify_signature(payload, None, SECRET) is False
    assert verify_signature(payload, "", SECRET) is False
    assert verify_signature(payload, "invalid_no_sha_prefix", SECRET) is False
    assert verify_signature(payload, "sha1=12345", SECRET) is False
