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

from app.agent.gemini_client import GeminiClient
from app.agent.graph import create_review_graph
from app.agent.schemas import ReviewFinding
from app.agent.state import AgentState
from app.ast_engine.context_builder import ASTContext, DiffChunk, build_ast_context_for_match
from app.ast_engine.index import RepoIndex
from app.ast_engine.symbols import map_hunks_to_symbols
from app.core.cache import ReviewCache
from app.core.config import Settings, get_settings
from app.core.diff_parser import parse_diff
from app.core.locks import PRLock, is_superseded
from app.core.logging import configure_logging
from app.core.prefilter import PullRequestMeta, run_prefilter
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
from app.github_client.client import GitHubClientProtocol, PyGithubClient, cleanup_pr_workspace

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
        """Execute the full autonomous code review, reproduction, and patching workflow."""
        repo = job.repo
        pr_number = job.pr_number
        head_sha = job.head_sha

        logger.info("Executing review pipeline for %s PR #%d (head: %s)", repo, pr_number, head_sha)

        # 1. Fetch PR metadata & unified diff from GitHub
        metadata = await self.github_client.get_pr_metadata(repo, pr_number)
        diff_text = await self.github_client.get_pr_diff(repo, pr_number)

        # 2. Zero-LLM static pre-filtering
        parsed_files = parse_diff(diff_text)
        pr_meta = PullRequestMeta(
            action="synchronize",
            draft=bool(metadata.get("is_draft", False)),
            author_login=str(metadata.get("author", "author")),
        )
        filter_result = run_prefilter(
            meta=pr_meta,
            files=parsed_files,
            max_changed_lines=self.settings.max_pr_changed_lines,
        )

        if filter_result.pr_skip is not None:
            logger.info("PR %s#%d skipped at PR-level: %s", repo, pr_number, filter_result.pr_skip)
            return {
                "status": "skipped",
                "reason": filter_result.pr_skip.value,
                "job_id": job.job_id,
            }

        if not filter_result.has_work:
            logger.info("All files in PR %s#%d filtered out as non-code/noise", repo, pr_number)
            return {
                "status": "skipped",
                "reason": "all_files_filtered",
                "job_id": job.job_id,
            }

        # 3. Download PR workspace snapshot into an ephemeral directory outside OneDrive
        workspace_dir, files_dict = await self.github_client.fetch_pr_workspace(repo, head_sha)

        try:
            # 4. AST symbol mapping & direct caller indexing
            repo_index = RepoIndex.build_from_files(files_dict)
            diff_chunks: list[DiffChunk] = []
            ast_contexts: list[ASTContext] = []

            for fdiff in filter_result.kept:
                head_code = files_dict.get(fdiff.path, "")
                matches = map_hunks_to_symbols(fdiff, head_code)
                for m in matches:
                    chunk, ast_ctx = build_ast_context_for_match(
                        match=m,
                        file_path=fdiff.path,
                        index=repo_index,
                        max_tokens=self.settings.max_context_tokens,
                        max_callers=self.settings.max_callers,
                    )
                    diff_chunks.append(chunk)
                    ast_contexts.append(ast_ctx)

            # 5. Check Redis review cache (Zero-LLM cache hit on identical chunks)
            cached_findings: list[ReviewFinding] = []
            all_cached = bool(diff_chunks)
            for chunk in diff_chunks:
                cached = await self.cache.get_review_chunk(
                    prompt_version=self.settings.prompt_version,
                    model=self.settings.gemini_model,
                    chunk_sha=chunk.chunk_sha,
                )
                if cached is not None:
                    for item in cached:
                        if isinstance(item, dict):
                            cached_findings.append(ReviewFinding.model_validate(item))
                        elif isinstance(item, ReviewFinding):
                            cached_findings.append(item)
                else:
                    all_cached = False

            if all_cached and cached_findings:
                logger.info("All review chunks hit Redis SHA diff cache. Bypassing LLM generation.")
                await self.github_client.post_pr_review_summary(
                    repo=repo,
                    pr_number=pr_number,
                    commit_sha=head_sha,
                    findings=cached_findings,
                    status="reviewed",
                )
                return {
                    "status": "reviewed",
                    "cached": True,
                    "findings_count": len(cached_findings),
                    "job_id": job.job_id,
                }

            # 6. Execute LangGraph review-reproduce-patch state machine
            gemini = GeminiClient(
                api_key=self.settings.gemini_api_key.get_secret_value(),
                model_name=self.settings.gemini_model,
                rate_limiter=self.rate_limiter,
                max_wait_s=self.settings.quota_max_wait_s,
            )
            graph = create_review_graph(gemini_client=gemini)
            initial_state: AgentState = {
                "repo_path": workspace_dir,
                "diff_hunks": diff_chunks,
                "ast_context": ast_contexts,
                "findings": [],
                "files": files_dict,
                "status": "running",
                "retry_count": 0,
                "max_retries": self.settings.max_retries,
                "llm_calls_used": 0,
            }

            final_state = await graph.ainvoke(initial_state)

            findings = final_state.get("findings", [])
            verified = final_state.get("verified", False)
            proposed_patch = final_state.get("proposed_patch")
            sandbox_result = final_state.get("sandbox_result")
            status = final_state.get("status", "reviewed")

            # 7. Post structured review comment to GitHub PR
            await self.github_client.post_pr_review_summary(
                repo=repo,
                pr_number=pr_number,
                commit_sha=head_sha,
                findings=findings,
                verified=verified,
                proposed_patch=proposed_patch,
                sandbox_result=sandbox_result,
                status=status,
            )

            # 8. Cache findings per chunk in Redis
            if findings:
                for chunk in diff_chunks:
                    chunk_findings = [f for f in findings if f.file_path == chunk.file_path]
                    await self.cache.set_review_chunk(
                        prompt_version=self.settings.prompt_version,
                        model=self.settings.gemini_model,
                        chunk_sha=chunk.chunk_sha,
                        findings=chunk_findings,
                        ttl_s=self.settings.review_cache_ttl_s,
                    )

            return {
                "status": status,
                "verified": verified,
                "findings_count": len(findings),
                "patch_proposed": proposed_patch is not None,
                "llm_calls_used": final_state.get("llm_calls_used", 0),
                "job_id": job.job_id,
            }

        finally:
            # 9. Guaranteed cleanup of temporary workspace outside OneDrive
            cleanup_pr_workspace(workspace_dir)

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
