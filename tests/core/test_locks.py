"""Unit tests for app.core.locks."""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.core.locks import (
    PRLock,
    acquire_pr_lock,
    extend_pr_lock,
    is_superseded,
    record_latest_head,
    release_pr_lock,
)


@pytest.mark.asyncio
async def test_pr_lock_acquire_and_release() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    # Acquire lock
    assert await acquire_pr_lock(redis, "org/repo", 1, "token-a", ttl_s=60) is True
    # Duplicate acquire fails
    assert await acquire_pr_lock(redis, "org/repo", 1, "token-b", ttl_s=60) is False

    # Release with wrong token fails
    assert await release_pr_lock(redis, "org/repo", 1, "wrong-token") is False
    # Release with correct token succeeds
    assert await release_pr_lock(redis, "org/repo", 1, "token-a") is True

    # Now can be acquired again
    assert await acquire_pr_lock(redis, "org/repo", 1, "token-b", ttl_s=60) is True


@pytest.mark.asyncio
async def test_pr_lock_extend() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    await acquire_pr_lock(redis, "org/repo", 1, "token-a", ttl_s=10)
    assert await extend_pr_lock(redis, "org/repo", 1, "wrong-token", ttl_s=30) is False
    assert await extend_pr_lock(redis, "org/repo", 1, "token-a", ttl_s=30) is True


@pytest.mark.asyncio
async def test_pr_lock_context_manager() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    async with PRLock(redis, "org/repo", 1, ttl_s=60, token="tok1"):
        # Contention inside context manager
        assert await acquire_pr_lock(redis, "org/repo", 1, "tok2", ttl_s=60) is False

    # After exit, lock is freed
    assert await acquire_pr_lock(redis, "org/repo", 1, "tok2", ttl_s=60) is True


@pytest.mark.asyncio
async def test_supersede_detection() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    # No record -> not superseded
    assert await is_superseded(redis, "org/repo", 1, "sha-1") is False

    await record_latest_head(redis, "org/repo", 1, "sha-1")
    assert await is_superseded(redis, "org/repo", 1, "sha-1") is False

    # New commit pushed to PR
    await record_latest_head(redis, "org/repo", 1, "sha-2")
    assert await is_superseded(redis, "org/repo", 1, "sha-1") is True
    assert await is_superseded(redis, "org/repo", 1, "sha-2") is False
