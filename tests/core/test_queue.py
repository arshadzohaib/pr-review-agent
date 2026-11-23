"""Unit tests for app.core.queue."""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.core.queue import (
    JobPayload,
    ack_job,
    dequeue_jobs,
    enqueue_delayed,
    enqueue_job,
    init_queue,
    move_delayed_jobs_to_stream,
    reclaim_stale_jobs,
)


@pytest.mark.asyncio
async def test_queue_enqueue_dequeue_ack() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await init_queue(redis)

    job = JobPayload(
        job_id="job-1",
        repo="org/repo",
        pr_number=42,
        head_sha="head123",
        base_sha="base123",
    )

    msg_id = await enqueue_job(redis, job)
    assert msg_id is not None

    received = await dequeue_jobs(redis, consumer_name="worker-1", count=5, block_ms=10)
    assert len(received) == 1
    rec_id, rec_job = received[0]
    assert rec_id == msg_id
    assert rec_job.job_id == "job-1"
    assert rec_job.pr_number == 42

    await ack_job(redis, rec_id)

    # Next read should be empty
    empty = await dequeue_jobs(redis, consumer_name="worker-1", count=5, block_ms=10)
    assert len(empty) == 0


@pytest.mark.asyncio
async def test_delayed_queue_movement() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await init_queue(redis)

    job = JobPayload(
        job_id="job-delayed",
        repo="org/repo",
        pr_number=10,
        head_sha="head1",
        base_sha="base1",
    )

    # Enqueue delayed with 5 seconds in future
    await enqueue_delayed(redis, job, delay_seconds=5.0)

    # Moving with now = current time shouldn't move it yet
    moved = await move_delayed_jobs_to_stream(redis, now=0.0)
    assert moved == 0

    # Moving with future timestamp should move it
    moved = await move_delayed_jobs_to_stream(redis, now=9999999999.0)
    assert moved == 1

    # Should now be in stream
    dequeued = await dequeue_jobs(redis, consumer_name="worker-1", block_ms=10)
    assert len(dequeued) == 1
    assert dequeued[0][1].job_id == "job-delayed"
    assert dequeued[0][1].delivery_count == 2


@pytest.mark.asyncio
async def test_reclaim_max_deliveries_to_dead_letter() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await init_queue(redis)

    # Job exceeding max deliveries (delivery_count=3, max_deliveries=3)
    dead_job = JobPayload(
        job_id="job-dead",
        repo="org/repo",
        pr_number=99,
        head_sha="h",
        base_sha="b",
        delivery_count=3,
    )
    await enqueue_job(redis, dead_job)
    # Read once to make it pending
    await dequeue_jobs(redis, consumer_name="worker-dead", block_ms=10)

    # Reclaim with 0 min_idle_ms
    reclaimed = await reclaim_stale_jobs(
        redis,
        consumer_name="worker-janitor",
        min_idle_ms=0,
        max_deliveries=3,
    )
    # It exceeded delivery count, so routed to dead letter and acked, not returned in active
    assert len(reclaimed) == 0

    # Dead stream should have the message
    dead_msgs = await redis.xlen("stream:jobs:dead")
    assert dead_msgs == 1
