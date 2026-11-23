"""Unit tests for background review worker."""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.core.cache import ReviewCache
from app.core.config import Settings
from app.core.locks import acquire_pr_lock, record_latest_head
from app.core.queue import JobPayload, enqueue_job, init_queue
from app.core.rate_limiter import GeminiRateLimiter
from app.worker import ReviewWorker


class FakeGitHubClient:
    async def get_pr_diff(self, repo: str, pr_number: int) -> str:
        return "diff --git a/a.py b/a.py\n"

    async def get_file_content(self, repo: str, file_path: str, ref: str) -> str | None:
        return "def foo(): pass"

    async def get_repo_archive(self, repo: str, ref: str) -> bytes:
        return b"fake_tarball_bytes"

    async def post_review(
        self,
        repo: str,
        pr_number: int,
        commit_sha: str,
        body: str,
        comments: list[dict[str, object]] | None = None,
        event: str = "COMMENT",
    ) -> int:
        return 12345


WorkerFixture = tuple[fakeredis.aioredis.FakeRedis, ReviewWorker]


@pytest.fixture
def worker_components(valid_env: dict[str, str]) -> WorkerFixture:
    settings = Settings(_env_file=None)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache = ReviewCache(redis)
    rate_limiter = GeminiRateLimiter(redis, rpm_limit=5, tpm_limit=50_000, rpd_limit=10)
    worker = ReviewWorker(
        redis=redis,
        cache=cache,
        rate_limiter=rate_limiter,
        github_client=FakeGitHubClient(),  # type: ignore[arg-type]
        settings=settings,
        consumer_name="test-worker-1",
    )
    return redis, worker


@pytest.mark.asyncio
async def test_worker_process_one_job_success(
    worker_components: tuple[fakeredis.aioredis.FakeRedis, ReviewWorker]
) -> None:
    redis, worker = worker_components
    await init_queue(redis)

    job = JobPayload(
        repo="octocat/hello-world",
        pr_number=1,
        head_sha="head-111",
        base_sha="base-000",
    )
    msg_id = await enqueue_job(redis, job)

    success = await worker.process_one_job(msg_id, job)
    assert success is True

    # Cache should record done
    done = await worker.cache.is_pr_head_done("octocat/hello-world", 1, "head-111")
    assert done is not None
    assert done.startswith("rev-")


@pytest.mark.asyncio
async def test_worker_skips_superseded_job(
    worker_components: tuple[fakeredis.aioredis.FakeRedis, ReviewWorker]
) -> None:
    redis, worker = worker_components
    await init_queue(redis)

    # Newer commit pushed to PR
    await record_latest_head(redis, "octocat/hello-world", 1, "head-new-222")

    job = JobPayload(
        repo="octocat/hello-world",
        pr_number=1,
        head_sha="head-old-111",
        base_sha="base-000",
    )
    msg_id = await enqueue_job(redis, job)

    success = await worker.process_one_job(msg_id, job)
    assert success is True

    # Was not reviewed (superseded)
    done = await worker.cache.is_pr_head_done("octocat/hello-world", 1, "head-old-111")
    assert done is None


@pytest.mark.asyncio
async def test_worker_skips_already_done_job(
    worker_components: tuple[fakeredis.aioredis.FakeRedis, ReviewWorker]
) -> None:
    redis, worker = worker_components
    await init_queue(redis)

    await worker.cache.mark_pr_head_done(
        "octocat/hello-world", 1, "head-111", review_id="rev-prior"
    )

    job = JobPayload(
        repo="octocat/hello-world",
        pr_number=1,
        head_sha="head-111",
        base_sha="base-000",
    )
    msg_id = await enqueue_job(redis, job)

    success = await worker.process_one_job(msg_id, job)
    assert success is True


@pytest.mark.asyncio
async def test_worker_handles_lock_contention(
    worker_components: tuple[fakeredis.aioredis.FakeRedis, ReviewWorker]
) -> None:
    redis, worker = worker_components
    await init_queue(redis)

    # Acquire lock externally
    await acquire_pr_lock(redis, "octocat/hello-world", 1, "other-worker-token", ttl_s=60)

    job = JobPayload(
        repo="octocat/hello-world",
        pr_number=1,
        head_sha="head-111",
        base_sha="base-000",
    )
    msg_id = await enqueue_job(redis, job)

    success = await worker.process_one_job(msg_id, job)
    assert success is True

    # Should have been delayed to jobs:delayed
    delayed_count = await redis.zcard("jobs:delayed")
    assert delayed_count == 1


@pytest.mark.asyncio
async def test_worker_handles_quota_exhausted(
    worker_components: tuple[fakeredis.aioredis.FakeRedis, ReviewWorker]
) -> None:
    redis, worker = worker_components
    await init_queue(redis)

    # Set rate limiter backoff active
    await worker.rate_limiter.record_backoff(worker.settings.gemini_model, delay_seconds=10.0)

    job = JobPayload(
        repo="octocat/hello-world",
        pr_number=1,
        head_sha="head-111",
        base_sha="base-000",
    )
    msg_id = await enqueue_job(redis, job)

    success = await worker.process_one_job(msg_id, job)
    assert success is True

    # Should have been delayed to jobs:delayed
    delayed_count = await redis.zcard("jobs:delayed")
    assert delayed_count == 1
