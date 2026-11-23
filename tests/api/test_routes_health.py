"""Unit tests for health and readiness routes."""

from __future__ import annotations

import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app


@pytest.mark.asyncio
async def test_health_endpoint() -> None:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

        resp_z = await client.get("/healthz")
        assert resp_z.status_code == 200
        assert resp_z.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_ready_endpoint_healthy() -> None:
    app = create_app()
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app.state.redis = redis

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"
        assert data["redis"] is True


@pytest.mark.asyncio
async def test_ready_endpoint_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app()
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    # Mock ping to fail
    async def mock_ping() -> bool:
        return False
    monkeypatch.setattr(redis, "ping", mock_ping)
    app.state.redis = redis

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/ready")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "unhealthy"
        assert data["redis"] is False
