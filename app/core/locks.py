"""Distributed PR locks and commit supersede tracking.

Provides:
- Atomic per-PR lock (SET NX PX) with unique token
- Atomic release (Lua compare-and-delete)
- Atomic heartbeat extension (Lua compare-and-extend)
- Supersede tracking: records latest seen head_sha for a PR and detects when a running job is stale
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from types import TracebackType
from typing import Any, Final, cast

from redis.asyncio import Redis

DEFAULT_LOCK_TTL_S: Final[int] = 300  # 5 minutes
DEFAULT_SUPERSEDE_TTL_S: Final[int] = 7 * 86_400  # 7 days

# Atomic compare-and-delete
_RELEASE_LUA: Final[str] = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

# Atomic compare-and-extend (ARGV[2] is milliseconds)
_EXTEND_LUA: Final[str] = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("pexpire", KEYS[1], ARGV[2])
else
    return 0
end
"""


def _lock_key(repo: str, pr: int) -> str:
    return f"lock:pr:{repo}:{pr}"


def _latest_head_key(repo: str, pr: int) -> str:
    return f"latest:pr:{repo}:{pr}"


async def acquire_pr_lock(
    client: Redis, repo: str, pr: int, token: str, ttl_s: int = DEFAULT_LOCK_TTL_S
) -> bool:
    """Acquire exclusive distributed lock for a PR."""
    key = _lock_key(repo, pr)
    result = await client.set(key, token, nx=True, ex=ttl_s)
    return bool(result)


async def release_pr_lock(client: Redis, repo: str, pr: int, token: str) -> bool:
    """Release distributed lock only if token matches."""
    key = _lock_key(repo, pr)
    eval_call = client.eval(_RELEASE_LUA, 1, key, token)
    result = await cast(Any, eval_call)
    return bool(result)


async def extend_pr_lock(
    client: Redis, repo: str, pr: int, token: str, ttl_s: int = DEFAULT_LOCK_TTL_S
) -> bool:
    """Extend lock TTL only if token matches."""
    key = _lock_key(repo, pr)
    ttl_ms = str(int(ttl_s * 1000))
    eval_call = client.eval(_EXTEND_LUA, 1, key, token, ttl_ms)
    result = await cast(Any, eval_call)
    return bool(result)


async def record_latest_head(
    client: Redis, repo: str, pr: int, head_sha: str, ttl_s: int = DEFAULT_SUPERSEDE_TTL_S
) -> None:
    """Record the latest head_sha seen for a PR (e.g. from synchronize events)."""
    key = _latest_head_key(repo, pr)
    await client.set(key, head_sha, ex=ttl_s)


async def is_superseded(client: Redis, repo: str, pr: int, head_sha: str) -> bool:
    """Check if a newer commit has superseded this job's head_sha."""
    key = _latest_head_key(repo, pr)
    latest = await client.get(key)
    return bool(latest is not None and latest != head_sha)


class PRLock:
    """Async context manager for per-PR distributed locking with optional heartbeat."""

    def __init__(
        self,
        client: Redis,
        repo: str,
        pr: int,
        ttl_s: int = DEFAULT_LOCK_TTL_S,
        token: str | None = None,
        heartbeat_interval_s: float | None = None,
    ) -> None:
        self.client = client
        self.repo = repo
        self.pr = pr
        self.ttl_s = ttl_s
        self.token = token or uuid.uuid4().hex
        self.heartbeat_interval_s = heartbeat_interval_s or max(ttl_s / 3.0, 1.0)
        self.acquired = False
        self._heartbeat_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> PRLock:
        self.acquired = await acquire_pr_lock(
            self.client, self.repo, self.pr, self.token, self.ttl_s
        )
        if not self.acquired:
            raise TimeoutError(f"Could not acquire lock for PR {self.repo}#{self.pr}")

        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None

        if self.acquired:
            await release_pr_lock(self.client, self.repo, self.pr, self.token)
            self.acquired = False

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval_s)
            extended = await extend_pr_lock(
                self.client, self.repo, self.pr, self.token, self.ttl_s
            )
            if not extended:
                break
