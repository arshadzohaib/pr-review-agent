"""Redis Streams job queue with consumer groups, delayed retry ZSET, and dead-letter queue.

Designed for Windows host & container worker environments without fork-based queues.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Final, cast

from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from redis.exceptions import ResponseError

DEFAULT_STREAM_NAME: Final[str] = "stream:jobs"
DEFAULT_GROUP_NAME: Final[str] = "workers"
DEFAULT_DELAYED_ZSET: Final[str] = "jobs:delayed"
DEFAULT_DEAD_STREAM: Final[str] = "stream:jobs:dead"

logger = logging.getLogger(__name__)


class JobPayload(BaseModel):
    """Payload representing an actionable pull-request review job."""

    model_config = ConfigDict(frozen=True)

    job_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    repo: str
    pr_number: int
    head_sha: str
    base_sha: str
    delivery_id: str = ""
    delivery_count: int = 1
    created_at: float = Field(default_factory=time.time)


async def init_queue(
    client: Redis,
    stream_name: str = DEFAULT_STREAM_NAME,
    group_name: str = DEFAULT_GROUP_NAME,
) -> None:
    """Initialize Redis Stream and Consumer Group if not already created."""
    try:
        await client.xgroup_create(stream_name, group_name, id="0", mkstream=True)
    except ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


async def enqueue_job(
    client: Redis,
    job: JobPayload,
    stream_name: str = DEFAULT_STREAM_NAME,
) -> str:
    """Enqueue a job to the Redis Stream. Returns the stream message ID."""
    entry: dict[Any, Any] = {"payload": job.model_dump_json()}
    msg_id = await client.xadd(stream_name, entry)
    return str(msg_id)


async def dequeue_jobs(
    client: Redis,
    consumer_name: str,
    stream_name: str = DEFAULT_STREAM_NAME,
    group_name: str = DEFAULT_GROUP_NAME,
    count: int = 1,
    block_ms: int = 2000,
) -> list[tuple[str, JobPayload]]:
    """Read pending jobs from consumer group. Returns list of (msg_id, JobPayload)."""
    try:
        raw: Any = await client.xreadgroup(
            group_name,
            consumer_name,
            streams={stream_name: ">"},
            count=count,
            block=block_ms,
        )
    except ResponseError:
        return []

    jobs: list[tuple[str, JobPayload]] = []
    if not raw:
        return jobs

    for _stream, messages in raw:
        for msg_id, fields in messages:
            payload_str = fields.get("payload")
            if not payload_str:
                continue
            try:
                job = JobPayload.model_validate_json(payload_str)
                jobs.append((str(msg_id), job))
            except Exception as exc:
                logger.warning("Failed to validate job payload from %s: %s", msg_id, exc)
                continue

    return jobs


async def ack_job(
    client: Redis,
    message_id: str,
    stream_name: str = DEFAULT_STREAM_NAME,
    group_name: str = DEFAULT_GROUP_NAME,
    delete_message: bool = True,
) -> None:
    """Acknowledge job completion and optionally delete it from the stream."""
    await client.xack(stream_name, group_name, message_id)
    if delete_message:
        await client.xdel(stream_name, message_id)


async def enqueue_delayed(
    client: Redis,
    job: JobPayload,
    delay_seconds: float,
    zset_name: str = DEFAULT_DELAYED_ZSET,
) -> None:
    """Enqueue a job into delayed ZSET with score = execution epoch timestamp."""
    ready_at = time.time() + delay_seconds
    retry_job = JobPayload(
        job_id=job.job_id,
        repo=job.repo,
        pr_number=job.pr_number,
        head_sha=job.head_sha,
        base_sha=job.base_sha,
        delivery_id=job.delivery_id,
        delivery_count=job.delivery_count + 1,
        created_at=job.created_at,
    )
    await client.zadd(zset_name, {retry_job.model_dump_json(): ready_at})


# Atomic Lua script to poll ready delayed jobs and push them back into stream
_MOVE_DELAYED_LUA: Final[str] = """
local zset_key = KEYS[1]
local stream_key = KEYS[2]
local now = tonumber(ARGV[1])
local limit = tonumber(ARGV[2]) or 10

local items = redis.call('zrangebyscore', zset_key, '-inf', now, 'LIMIT', 0, limit)
local moved = 0

for _, payload in ipairs(items) do
    redis.call('xadd', stream_key, '*', 'payload', payload)
    redis.call('zrem', zset_key, payload)
    moved = moved + 1
end

return moved
"""


async def move_delayed_jobs_to_stream(
    client: Redis,
    stream_name: str = DEFAULT_STREAM_NAME,
    zset_name: str = DEFAULT_DELAYED_ZSET,
    now: float | None = None,
    limit: int = 10,
) -> int:
    """Move ready delayed jobs from ZSET to the stream. Returns count of moved jobs."""
    current_time = str(now if now is not None else time.time())
    eval_call = client.eval(
        _MOVE_DELAYED_LUA, 2, zset_name, stream_name, current_time, str(limit)
    )
    res = await cast(Any, eval_call)
    return int(res or 0)


async def reclaim_stale_jobs(
    client: Redis,
    consumer_name: str,
    min_idle_ms: int = 600_000,
    stream_name: str = DEFAULT_STREAM_NAME,
    group_name: str = DEFAULT_GROUP_NAME,
    max_deliveries: int = 3,
    dead_stream: str = DEFAULT_DEAD_STREAM,
) -> list[tuple[str, JobPayload]]:
    """Reclaim unacknowledged jobs from stalled workers using XAUTOCLAIM.

    Jobs exceeding max_deliveries are moved to dead-letter stream and acknowledged.
    """
    try:
        res: Any = await client.xautoclaim(
            stream_name,
            group_name,
            consumer_name,
            min_idle_time=min_idle_ms,
            start_id="0-0",
            count=10,
        )
    except ResponseError:
        return []

    if not res or len(res) < 2:
        return []

    messages = res[1]
    active_reclaimed: list[tuple[str, JobPayload]] = []

    for item in messages:
        if not item or len(item) < 2:
            continue
        msg_id, fields = item[0], item[1]
        payload_str = fields.get("payload") if isinstance(fields, dict) else None
        if not payload_str:
            continue

        try:
            job = JobPayload.model_validate_json(payload_str)
        except Exception:
            await ack_job(client, str(msg_id), stream_name, group_name)
            continue

        if job.delivery_count >= max_deliveries:
            dead_entry: dict[Any, Any] = {
                "payload": payload_str,
                "reason": "max_deliveries_exceeded",
            }
            await client.xadd(dead_stream, dead_entry)
            await ack_job(client, str(msg_id), stream_name, group_name)
        else:
            active_reclaimed.append((str(msg_id), job))

    return active_reclaimed
