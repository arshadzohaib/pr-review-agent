"""Unit tests for GitHub webhook endpoint."""

from __future__ import annotations

import json
from typing import Any

import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient

from app.api.security import compute_signature
from app.core.cache import ReviewCache
from app.core.config import Settings
from app.core.queue import init_queue
from app.main import create_app
from tests.conftest import VALID_SECRETS

WEBHOOK_SECRET = VALID_SECRETS["GITHUB_WEBHOOK_SECRET"]


def _make_pr_payload(
    action: str = "opened",
    number: int = 42,
    head_sha: str = "abc123head",
    base_sha: str = "def456base",
    draft: bool = False,
    login: str = "alice",
    labels: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "action": action,
        "number": number,
        "pull_request": {
            "number": number,
            "title": "Fix calculation logic",
            "state": "open",
            "draft": draft,
            "user": {"login": login, "type": "User"},
            "head": {
                "sha": head_sha,
                "ref": "fix-bug",
                "repo": {"full_name": "octocat/hello-world"},
            },
            "base": {
                "sha": base_sha,
                "ref": "main",
                "repo": {"full_name": "octocat/hello-world"},
            },
            "labels": [{"name": lbl} for lbl in (labels or [])],
        },
        "repository": {
            "full_name": "octocat/hello-world",
            "name": "hello-world",
            "private": False,
        },
    }


@pytest.fixture
def test_app(valid_env: dict[str, str]) -> Any:
    app = create_app()
    settings = Settings(_env_file=None)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache = ReviewCache(redis)

    app.state.settings = settings
    app.state.redis = redis
    app.state.cache = cache
    return app


@pytest.mark.asyncio
async def test_webhook_unauthorized_missing_or_bad_sig(test_app: Any) -> None:
    payload_bytes = json.dumps({"action": "opened"}).encode("utf-8")

    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        # Missing signature
        resp1 = await client.post(
            "/webhook/github",
            content=payload_bytes,
            headers={"X-GitHub-Event": "pull_request"},
        )
        assert resp1.status_code == 401

        # Invalid signature
        resp2 = await client.post(
            "/webhook/github",
            content=payload_bytes,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": "sha256=invalidhex",
            },
        )
        assert resp2.status_code == 401


@pytest.mark.asyncio
async def test_webhook_ping_event(test_app: Any) -> None:
    payload_bytes = json.dumps({"zen": "Keep it logically awesome."}).encode("utf-8")
    sig = compute_signature(payload_bytes, WEBHOOK_SECRET)

    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        resp = await client.post(
            "/webhook/github",
            content=payload_bytes,
            headers={
                "X-GitHub-Event": "ping",
                "X-Hub-Signature-256": sig,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert resp.json()["message"] == "pong"


@pytest.mark.asyncio
async def test_webhook_ignored_event(test_app: Any) -> None:
    payload_bytes = json.dumps({"action": "created"}).encode("utf-8")
    sig = compute_signature(payload_bytes, WEBHOOK_SECRET)

    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        resp = await client.post(
            "/webhook/github",
            content=payload_bytes,
            headers={
                "X-GitHub-Event": "issues",
                "X-Hub-Signature-256": sig,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_webhook_delivery_deduplication(test_app: Any) -> None:
    payload = _make_pr_payload()
    payload_bytes = json.dumps(payload).encode("utf-8")
    sig = compute_signature(payload_bytes, WEBHOOK_SECRET)
    delivery_id = "deliv-unique-guid-123"

    headers = {
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": sig,
    }

    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        # First delivery -> 202 Accepted
        resp1 = await client.post("/webhook/github", content=payload_bytes, headers=headers)
        assert resp1.status_code == 202
        assert resp1.json()["status"] == "enqueued"

        # Duplicate delivery -> 200 OK duplicate
        resp2 = await client.post("/webhook/github", content=payload_bytes, headers=headers)
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "duplicate"


@pytest.mark.asyncio
async def test_webhook_skipped_pr(test_app: Any) -> None:
    # Draft PR
    payload = _make_pr_payload(draft=True)
    payload_bytes = json.dumps(payload).encode("utf-8")
    sig = compute_signature(payload_bytes, WEBHOOK_SECRET)

    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        resp = await client.post(
            "/webhook/github",
            content=payload_bytes,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "deliv-draft",
                "X-Hub-Signature-256": sig,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "skipped"
        assert resp.json()["details"]["skip_reason"] == "draft"


@pytest.mark.asyncio
async def test_webhook_cached_pr_head(test_app: Any) -> None:
    cache: ReviewCache = test_app.state.cache
    await cache.mark_pr_head_done(
        "octocat/hello-world", 42, "head-already-done", review_id="rev-old-1"
    )

    payload = _make_pr_payload(number=42, head_sha="head-already-done")
    payload_bytes = json.dumps(payload).encode("utf-8")
    sig = compute_signature(payload_bytes, WEBHOOK_SECRET)

    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        resp = await client.post(
            "/webhook/github",
            content=payload_bytes,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "deliv-cached",
                "X-Hub-Signature-256": sig,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "cached"
        assert resp.json()["details"]["review_id"] == "rev-old-1"


@pytest.mark.asyncio
async def test_webhook_successful_enqueue(test_app: Any) -> None:
    redis = test_app.state.redis
    await init_queue(redis)

    payload = _make_pr_payload(number=10, head_sha="fresh-sha-99")
    payload_bytes = json.dumps(payload).encode("utf-8")
    sig = compute_signature(payload_bytes, WEBHOOK_SECRET)

    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/github",  # Test alternate plural route
            content=payload_bytes,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "deliv-success-1",
                "X-Hub-Signature-256": sig,
            },
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "enqueued"
        assert body["job_id"] is not None

        # Verify stream contains 1 message
        assert await redis.xlen("stream:jobs") == 1
        # Verify latest head recorded
        latest = await redis.get("latest:pr:octocat/hello-world:10")
        assert latest == "fresh-sha-99"
