"""Health and readiness check endpoints for container orchestration."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request, Response, status

from app.core.config import get_settings
from app.core.redis_client import create_redis
from app.core.redis_client import ping as ping_redis

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health", status_code=status.HTTP_200_OK)
@router.get("/healthz", status_code=status.HTTP_200_OK)
async def health_check() -> dict[str, str]:
    """Lightweight liveness probe."""
    return {"status": "ok"}


@router.get("/ready", status_code=status.HTTP_200_OK)
@router.get("/readyz", status_code=status.HTTP_200_OK)
async def readiness_check(request: Request, response: Response) -> dict[str, Any]:
    """Readiness probe checking Redis connectivity."""
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        settings = getattr(request.app.state, "settings", None) or get_settings()
        redis = create_redis(settings.redis_url)
        request.app.state.redis = redis

    redis_ok = await ping_redis(redis)

    docker_ok: bool | None = None
    # Check docker if client is mounted on app.state
    docker_client = getattr(request.app.state, "docker_client", None)
    if docker_client is not None:
        try:
            docker_ok = bool(docker_client.ping())
        except Exception:
            docker_ok = False

    if not redis_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "unhealthy",
            "redis": False,
            "docker": docker_ok,
            "detail": "Redis connection failed",
        }

    return {
        "status": "ready",
        "redis": True,
        "docker": docker_ok,
    }
