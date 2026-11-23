"""FastAPI application entry point and lifespan manager."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from app.api import routes_health, routes_webhook
from app.core.cache import ReviewCache
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.queue import init_queue
from app.core.rate_limiter import GeminiRateLimiter
from app.core.redis_client import create_redis


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[dict[str, Any]]:
    """Initialize Redis pool, stream queue, cache, and rate limiter on startup."""
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

    app.state.settings = settings
    app.state.redis = redis
    app.state.cache = cache
    app.state.rate_limiter = rate_limiter

    yield {}

    await redis.aclose()


def create_app() -> FastAPI:
    """Build and configure the FastAPI web application."""
    application = FastAPI(
        title="Autonomous PR Review & Patching Agent",
        description=(
            "Autonomous GitHub PR code-review, bug-finding, and patching agent "
            "on free-tier Gemini."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    application.include_router(routes_health.router)
    application.include_router(routes_webhook.router)

    return application


app = create_app()
