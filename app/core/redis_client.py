"""Async Redis client factory. All keys/values are ``str`` (``decode_responses=True``)."""

from __future__ import annotations

from redis.asyncio import ConnectionPool, Redis


def create_redis(
    url: str,
    *,
    max_connections: int = 20,
    socket_timeout_s: float = 10.0,
    socket_connect_timeout_s: float = 5.0,
) -> Redis:
    """Build a pooled async client. Does not connect until the first command."""
    pool: ConnectionPool = ConnectionPool.from_url(
        url,
        max_connections=max_connections,
        decode_responses=True,
        socket_timeout=socket_timeout_s,
        socket_connect_timeout=socket_connect_timeout_s,
        health_check_interval=30,
    )
    return Redis(connection_pool=pool)


async def ping(client: Redis) -> bool:
    """Return True if Redis answers PING (never raises on connection problems)."""
    try:
        return bool(await client.ping())
    except Exception:
        return False
