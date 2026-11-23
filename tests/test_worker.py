"""Unit tests for background review worker."""

from __future__ import annotations

import tempfile
from typing import Any
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest

from app.agent.schemas import PatchProposal, ReviewFinding, ReviewResponse
from app.core.cache import ReviewCache
from app.core.config import Settings
from app.core.locks import acquire_pr_lock, record_latest_head
from app.core.queue import JobPayload, enqueue_job, init_queue
from app.core.rate_limiter import GeminiRateLimiter
from app.sandbox.models import SandboxResult
from app.worker import ReviewWorker


class FakeGitHubClient:
    def __init__(self) -> None:
        self.posted_reviews: list[dict[str, Any]] = []

    async def get_pr_metadata(self, repo: str, pr_number: int) -> dict[str, Any]:
        return {
            "title": "Fix math addition",
            "body": "Fixes off by one error",
            "is_draft": False,
            "author": "developer",
            "base_sha": "base-000",
            "head_sha": "head-111",
        }

    async def get_pr_diff(self, repo: str, pr_number: int) -> str:
        return (
            "diff --git a/calc.py b/calc.py\n"
            "--- a/calc.py\n"
            "+++ b/calc.py\n"
            "@@ -1 +1 @@\n"
            "-def add(a, b): return a - b\n"
            "+def add(a, b): return a + b\n"
        )

    async def get_file_content(self, repo: str, file_path: str, ref: str) -> str | None:
        return "def add(a, b): return a + b\n"

    async def get_repo_archive(self, repo: str, ref: str) -> bytes:
        return b"fake_tarball_bytes"

    async def fetch_pr_workspace(self, repo: str, ref: str) -> tuple[str, dict[str, str]]:
        temp_dir = tempfile.mkdtemp(prefix="test_pr_repo_")
        files = {"calc.py": "def add(a, b):\n    return a + b\n"}
        return temp_dir, files

    async def has_bot_reviewed(self, repo: str, pr_number: int, commit_sha: str) -> bool:
        return False

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

    async def post_pr_review_summary(
        self,
        repo: str,
        pr_number: int,
        commit_sha: str,
        findings: list[ReviewFinding],
        verified: bool = False,
        proposed_patch: PatchProposal | None = None,
        sandbox_result: SandboxResult | None = None,
        status: str = "reviewed",
    ) -> int:
        self.posted_reviews.append({
            "repo": repo,
            "pr_number": pr_number,
            "commit_sha": commit_sha,
            "findings": findings,
            "verified": verified,
            "status": status,
        })
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

    clean_review = ReviewResponse(findings=[], summary="Looks great")
    patch_target = "app.agent.gemini_client.GeminiClient.generate_structured"
    with patch(patch_target, new_callable=AsyncMock) as mock_gen:
        mock_gen.return_value = clean_review
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


@pytest.mark.asyncio
async def test_worker_execute_pipeline_skips_when_all_files_filtered(
    worker_components: tuple[fakeredis.aioredis.FakeRedis, ReviewWorker]
) -> None:
    _, worker = worker_components

    # Override diff to return only markdown/doc file (zero-LLM pre-filter skips it)
    async def _doc_diff(repo: str, pr_number: int) -> str:
        return "diff --git a/README.md b/README.md\n@@ -1 +1 @@\n-# Title\n+# New Title\n"

    worker.github_client.get_pr_diff = _doc_diff  # type: ignore[method-assign]

    job = JobPayload(
        repo="octocat/hello-world",
        pr_number=1,
        head_sha="head-111",
        base_sha="base-000",
    )
    result = await worker.execute_review_pipeline(job)
    assert result["status"] == "skipped"
    assert result["reason"] == "all_files_filtered"


@pytest.mark.asyncio
async def test_worker_execute_pipeline_cache_hit_bypasses_llm(
    worker_components: tuple[fakeredis.aioredis.FakeRedis, ReviewWorker]
) -> None:
    _, worker = worker_components

    # Pre-populate review cache for the hunk's chunk_sha
    fake_finding = ReviewFinding(
        file_path="calc.py",
        line=1,
        title="Cached finding",
        explanation="From previous run",
    )

    # First run to cache the review
    clean_review = ReviewResponse(findings=[fake_finding], summary="Cached test")
    with patch(
        "app.agent.gemini_client.GeminiClient.generate_structured",
        new_callable=AsyncMock,
    ) as mock_gen:
        mock_gen.return_value = clean_review
        job = JobPayload(
            repo="octocat/hello-world",
            pr_number=1,
            head_sha="head-111",
            base_sha="base-000",
        )
        res1 = await worker.execute_review_pipeline(job)
        assert res1["status"] == "clean" or res1["status"] == "reviewed"

    # Second run with same diff chunks should hit cache without calling LLM
    with patch(
        "app.agent.gemini_client.GeminiClient.generate_structured",
        new_callable=AsyncMock,
    ) as mock_gen2:
        res2 = await worker.execute_review_pipeline(job)
        assert res2["status"] == "reviewed"
        assert res2.get("cached") is True
        mock_gen2.assert_not_called()

