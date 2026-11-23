"""Background worker process popping and executing review jobs from Redis Streams.

Handles:
- Consumer group dequeue from 'stream:jobs'
- Stale job superseding detection
- Distributed PR lock acquisition with heartbeat
- Atomic Lua Gemini quota checks & delayed re-enqueue
- Execution of the review workflow (stub in Phase 3, LangGraph in Phase 6)
- Graceful shutdown on SIGINT/SIGTERM
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import uuid
from typing import Any

from redis.asyncio import Redis

from app.core.cache import ReviewCache
from app.core.config import Settings, get_settings
from app.core.locks import PRLock, is_superseded
from app.core.logging import configure_logging
from app.core.queue import (
    JobPayload,
    ack_job,
    dequeue_jobs,
    enqueue_delayed,
    init_queue,
    move_delayed_jobs_to_stream,
    reclaim_stale_jobs,
)
from app.core.rate_limiter import GeminiRateLimiter
from app.core.redis_client import create_redis
from app.integrations.github_client import GitHubClientProtocol, PyGithubClient

logger = logging.getLogger(__name__)


class ReviewWorker:
    """Worker instance processing pull request review jobs."""

    def __init__(
        self,
        redis: Redis,
        cache: ReviewCache,
        rate_limiter: GeminiRateLimiter,
        github_client: GitHubClientProtocol | None = None,
        settings: Settings | None = None,
        consumer_name: str | None = None,
    ) -> None:
        self.redis = redis
        self.cache = cache
        self.rate_limiter = rate_limiter
        self.settings = settings or get_settings()
        self.github_client = github_client or PyGithubClient(
            self.settings.github_token.get_secret_value()
        )
        self.consumer_name = consumer_name or f"worker-{uuid.uuid4().hex[:8]}"
        self.running = False
        self._last_reclaim_time = 0.0

    async def execute_review_pipeline(self, job: JobPayload) -> dict[str, Any]:
        """Execute the code review workflow.

        In Phase 3: Stub workflow validating pipeline wiring.
        In Phase 6: Wires full LangGraph state machine.
        """
        logger.info(
            "Executing review pipeline for %s PR #%d (head: %s)",
            job.repo,
            job.pr_number,
            job.head_sha,
        )
        # Simulation of pipeline processing
        await asyncio.sleep(0.01)
        return {
            "status": "reviewed",
            "findings_count": 0,
            "job_id": job.job_id,
        }

    async def process_one_job(self, msg_id: str, job: JobPayload) -> bool:
        """Process a single job item from Redis stream. Returns True if job was handled/acked."""
        repo = job.repo
        pr_number = job.pr_number
        head_sha = job.head_sha

        # 1. Check if PR was superseded by a newer commit
        if await is_superseded(self.redis, repo, pr_number, head_sha):
            logger.info(
                "Job %s for %s#%d superseded by newer commit; skipping",
                job.job_id,
                repo,
                pr_number,
            )
            await ack_job(self.redis, msg_id)
            return True

        # 2. Check if head commit was already reviewed
        if await self.cache.is_pr_head_done(repo, pr_number, head_sha) is not None:
            logger.info(
                "Job %s for %s#%d (%s) already completed; skipping",
                job.job_id,
                repo,
                pr_number,
                head_sha,
            )
            await ack_job(self.redis, msg_id)
            return True

        # 3. Check Gemini Rate Limiter quota before running LLM operations
        quota_res = await self.rate_limiter.acquire(
            model=self.settings.gemini_model, estimated_tokens=1000
        )
        if not quota_res.allowed:
            logger.warning(
                "Gemini quota unavailable (%s). Delaying job %s by %.1fs",
                quota_res.reason,
                job.job_id,
                quota_res.retry_after_s,
            )
            # Re-queue into delayed ZSET and ack current stream entry
            await enqueue_delayed(self.redis, job, delay_seconds=max(quota_res.retry_after_s, 5.0))
            await ack_job(self.redis, msg_id)
            return True

        # 4. Acquire distributed lock for this PR
        try:
            async with PRLock(self.redis, repo, pr_number, ttl_s=self.settings.lock_ttl_s):
                # 5. Execute review pipeline
                review_result = await self.execute_review_pipeline(job)

                # 6. Mark done in cache & acknowledge stream message
                review_id = f"rev-{job.job_id}"
                await self.cache.mark_pr_head_done(repo, pr_number, head_sha, review_id=review_id)
                await ack_job(self.redis, msg_id)
                logger.info(
                    "Successfully completed job %s for %s#%d: %s",
                    job.job_id,
                    repo,
                    pr_number,
                    review_result.get("status"),
                )
                return True
        except TimeoutError:
            logger.warning(
                "Could not acquire lock for %s#%d (another worker active); delaying",
                repo,
                pr_number,
            )
            await enqueue_delayed(self.redis, job, delay_seconds=5.0)
            await ack_job(self.redis, msg_id)
            return True
        except Exception as exc:
            logger.error("Error processing job %s: %s", job.job_id, exc, exc_info=True)
            # Re-enqueue with delay if delivery count within limit
            if job.delivery_count < self.settings.queue_max_deliveries:
                await enqueue_delayed(self.redis, job, delay_seconds=10.0)
            await ack_job(self.redis, msg_id)
            return False

    async def run_loop(self) -> None:
        """Run continuous worker loop until stopped."""
        self.running = True
        logger.info("Starting review worker [%s]", self.consumer_name)

        while self.running:
            try:
                # 1. Move any ready delayed jobs back into stream
                await move_delayed_jobs_to_stream(self.redis)

                # 2. Dequeue pending jobs
                jobs = await dequeue_jobs(
                    self.redis,
                    consumer_name=self.consumer_name,
                    count=1,
                    block_ms=1000,
                )

                for msg_id, job in jobs:
                    await self.process_one_job(msg_id, job)

                # 3. Periodically reclaim stale jobs from dead consumers
                now = asyncio.get_event_loop().time()
                if now - self._last_reclaim_time > 60.0:
                    self._last_reclaim_time = now
                    stale = await reclaim_stale_jobs(
                        self.redis,
                        consumer_name=self.consumer_name,
                        min_idle_ms=self.settings.queue_stale_after_s * 1000,
                        max_deliveries=self.settings.queue_max_deliveries,
                    )
                    for stale_id, stale_job in stale:
                        await self.process_one_job(stale_id, stale_job)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Unhandled error in worker loop: %s", exc, exc_info=True)
                await asyncio.sleep(1.0)

        logger.info("Worker [%s] stopped cleanly", self.consumer_name)

    def stop(self) -> None:
        """Signal worker to stop processing."""
        self.running = False


async def run_worker() -> None:
    """Worker process entrypoint."""
    settings = get_settings()
    configure_logging(settings)

    redis = create_redis(settings.redis_url)
    await init_queue(redis)

    cache = ReviewCache(redis, default_review_ttl_s=settings.review_cache_ttl_s)
    rate_limiter = GeminiRateLimiter(
        redis,
        default_model=settings.gemini_model,
        rpm_limit=settings.gemini_rpm_limit,
        tpm_limit=settings.gemini_tpm_limit,
        rpd_limit=settings.gemini_rpd_limit,
    )
    worker = ReviewWorker(redis, cache, rate_limiter, settings=settings)

    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        worker.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _signal_handler)

    try:
        await worker.run_loop()
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(run_worker())
