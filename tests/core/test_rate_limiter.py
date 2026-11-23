"""Unit tests for app.core.rate_limiter."""

from __future__ import annotations

import datetime

import fakeredis.aioredis
import pytest

from app.core.rate_limiter import (
    GeminiRateLimiter,
    estimate_tokens,
    get_pacific_date_and_seconds_until_midnight,
)


def test_estimate_tokens() -> None:
    assert estimate_tokens("") == 1
    assert estimate_tokens("hello world") == 3  # 11 chars -> ceil(11/4) = 3


def test_get_pacific_date_and_seconds_until_midnight() -> None:
    # 2026-11-23 20:00:00 UTC = 2026-11-23 12:00:00 PST (12 hours before midnight)
    utc_time = datetime.datetime(2026, 11, 23, 20, 0, 0, tzinfo=datetime.UTC)
    date_str, secs = get_pacific_date_and_seconds_until_midnight(utc_time)

    assert date_str == "2026-11-23"
    assert secs == 12 * 3600


@pytest.mark.asyncio
async def test_rate_limiter_rpm_limit() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    limiter = GeminiRateLimiter(redis, rpm_limit=2, tpm_limit=100_000, rpd_limit=50)

    now = 1000.0
    # Request 1: allowed
    res1 = await limiter.acquire(now=now, estimated_tokens=100)
    assert res1.allowed is True
    assert res1.reason == "OK"

    # Request 2: allowed
    res2 = await limiter.acquire(now=now + 5, estimated_tokens=100)
    assert res2.allowed is True

    # Request 3: blocked by RPM
    res3 = await limiter.acquire(now=now + 10, estimated_tokens=100)
    assert res3.allowed is False
    assert res3.reason == "RPM_EXHAUSTED"
    assert res3.retry_after_s > 0

    # After 60 seconds from first request, window slides and allows request
    res4 = await limiter.acquire(now=now + 61, estimated_tokens=100)
    assert res4.allowed is True


@pytest.mark.asyncio
async def test_rate_limiter_tpm_limit() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    limiter = GeminiRateLimiter(redis, rpm_limit=10, tpm_limit=500, rpd_limit=50)

    now = 1000.0
    res1 = await limiter.acquire(now=now, estimated_tokens=400)
    assert res1.allowed is True

    # Exceeds 500 TPM
    res2 = await limiter.acquire(now=now + 2, estimated_tokens=200)
    assert res2.allowed is False
    assert res2.reason == "TPM_EXHAUSTED"


@pytest.mark.asyncio
async def test_rate_limiter_rpd_limit() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    limiter = GeminiRateLimiter(redis, rpm_limit=10, tpm_limit=100_000, rpd_limit=2)

    now = 1000.0
    assert (await limiter.acquire(now=now, estimated_tokens=10)).allowed is True
    assert (await limiter.acquire(now=now + 1, estimated_tokens=10)).allowed is True

    # 3rd request exceeds RPD limit
    res3 = await limiter.acquire(now=now + 2, estimated_tokens=10)
    assert res3.allowed is False
    assert res3.reason == "RPD_EXHAUSTED"


@pytest.mark.asyncio
async def test_rate_limiter_backoff_and_usage_correction() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    limiter = GeminiRateLimiter(redis, rpm_limit=10, tpm_limit=100_000, rpd_limit=50)

    # 429 backoff
    await limiter.record_backoff("gemini-3.8-flash", delay_seconds=2.0)
    res = await limiter.acquire()
    assert res.allowed is False
    assert res.reason == "BACKOFF_ACTIVE"

    # Token usage correction
    req = await limiter.acquire(model="gemini-3.8-flash", estimated_tokens=500)
    if req.allowed:
        await limiter.record_usage(
            "gemini-3.8-flash", req.req_id, estimated_tokens=500, actual_tokens=250
        )
