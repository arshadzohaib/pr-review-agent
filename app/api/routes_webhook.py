"""GitHub Webhook router for incoming PR events.

Implements:
- Strict HMAC-SHA256 signature verification
- Event and action filtering (pull_request opened, synchronize, reopened, ready_for_review)
- Delivery GUID deduplication via Redis cache
- Commit supersede tracking
- Redis Stream job enqueue with 202 Accepted response
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status

from app.api.schemas import PullRequestWebhookEvent, WebhookResponse
from app.api.security import verify_github_webhook
from app.core.cache import ReviewCache
from app.core.config import get_settings
from app.core.locks import record_latest_head
from app.core.prefilter import PullRequestMeta, check_event
from app.core.queue import JobPayload, enqueue_job
from app.core.redis_client import create_redis

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])


def _get_cache(request: Request) -> ReviewCache:
    cache = getattr(request.app.state, "cache", None)
    if cache is not None:
        return cache  # type: ignore[no-any-return]
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        settings = getattr(request.app.state, "settings", None) or get_settings()
        redis = create_redis(settings.redis_url)
        request.app.state.redis = redis
    cache = ReviewCache(redis)
    request.app.state.cache = cache
    return cache


async def handle_github_webhook(
    request: Request,
    response: Response,
    body: bytes = Depends(verify_github_webhook),
    x_github_event: str | None = Header(None, alias="X-GitHub-Event"),
    x_github_delivery: str | None = Header(None, alias="X-GitHub-Delivery"),
) -> WebhookResponse:
    """Handle incoming webhook payload from GitHub."""
    delivery_id = x_github_delivery or ""
    cache = _get_cache(request)
    redis = cache.client

    # 1. Event verification
    if not x_github_event:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing X-GitHub-Event header",
        )

    if x_github_event == "ping":
        response.status_code = status.HTTP_200_OK
        return WebhookResponse(status="ok", message="pong", delivery_id=delivery_id)

    if x_github_event != "pull_request":
        response.status_code = status.HTTP_200_OK
        return WebhookResponse(
            status="ignored",
            message=f"Event '{x_github_event}' is not handled",
            delivery_id=delivery_id,
        )

    # 2. Delivery deduplication
    if delivery_id:
        is_new = await cache.check_and_set_delivery(delivery_id)
        if not is_new:
            response.status_code = status.HTTP_200_OK
            return WebhookResponse(
                status="duplicate",
                message="Delivery GUID already processed",
                delivery_id=delivery_id,
            )

    # 3. Payload validation
    try:
        event = PullRequestWebhookEvent.model_validate_json(body)
    except Exception as exc:
        logger.warning("Failed to parse pull_request webhook payload: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Malformed pull_request payload: {exc}",
        ) from exc

    # 4. Zero-LLM Event Pre-Filter
    meta = PullRequestMeta(
        action=event.action,
        draft=event.pull_request.draft,
        author_login=event.pull_request.user.login,
        author_type=event.pull_request.user.type,
        labels=event.label_names,
    )
    skip_reason = check_event(meta)
    if skip_reason is not None:
        response.status_code = status.HTTP_200_OK
        return WebhookResponse(
            status="skipped",
            message=f"PR skipped by pre-filter: {skip_reason.value}",
            delivery_id=delivery_id,
            details={"action": event.action, "skip_reason": skip_reason.value},
        )

    repo = event.repo_full_name
    pr_number = event.number
    head_sha = event.head_sha
    base_sha = event.base_sha

    # 5. Check if head commit was already reviewed
    completed_review = await cache.is_pr_head_done(repo, pr_number, head_sha)
    if completed_review is not None:
        response.status_code = status.HTTP_200_OK
        return WebhookResponse(
            status="cached",
            message="Commit head already reviewed",
            delivery_id=delivery_id,
            details={"head_sha": head_sha, "review_id": completed_review},
        )

    # 6. Record latest seen head to supersede prior stale in-flight jobs
    await record_latest_head(redis, repo, pr_number, head_sha)

    # 7. Enqueue review job into Redis stream
    job = JobPayload(
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        base_sha=base_sha,
        delivery_id=delivery_id,
    )
    msg_id = await enqueue_job(redis, job)

    response.status_code = status.HTTP_202_ACCEPTED
    return WebhookResponse(
        status="enqueued",
        message="Review job enqueued successfully",
        delivery_id=delivery_id,
        job_id=job.job_id,
        details={
            "msg_id": msg_id,
            "repo": repo,
            "pr_number": pr_number,
            "head_sha": head_sha,
        },
    )


# Register for both /webhook/github and /webhooks/github endpoints
router.add_api_route(
    "/webhook/github",
    handle_github_webhook,
    methods=["POST"],
    response_model=WebhookResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
router.add_api_route(
    "/webhooks/github",
    handle_github_webhook,
    methods=["POST"],
    response_model=WebhookResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
