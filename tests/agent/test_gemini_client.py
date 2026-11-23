"""Unit tests for GeminiClient and rate limiting integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from app.agent.gemini_client import GeminiClient
from app.core.rate_limiter import GeminiRateLimiter, RateLimitResult


class DummyOutput(BaseModel):
    message: str
    code: int


@pytest.mark.asyncio
async def test_gemini_client_successful_structured_generation() -> None:
    client = GeminiClient(api_key="fake-test-key", model_name="gemini-2.5-flash")

    mock_response = MagicMock()
    mock_response.text = '{"message": "success", "code": 200}'
    mock_response.parsed = None

    mock_genai_client = MagicMock()
    mock_genai_client.models.generate_content.return_value = mock_response

    client._client = mock_genai_client

    result = await client.generate_structured(
        prompt="hello",
        schema=DummyOutput,
    )

    assert result.message == "success"
    assert result.code == 200


@pytest.mark.asyncio
async def test_gemini_client_rate_limiter_blocks_on_rpd() -> None:
    mock_limiter = AsyncMock(spec=GeminiRateLimiter)
    mock_limiter.acquire.return_value = RateLimitResult(
        allowed=False,
        reason="RPD_EXHAUSTED",
        retry_after_s=3600.0,
    )

    client = GeminiClient(
        api_key="fake-key",
        rate_limiter=mock_limiter,
    )

    with pytest.raises(RuntimeError, match="Gemini daily quota \\(RPD\\) exhausted"):
        await client.generate_structured("prompt", schema=DummyOutput)


@pytest.mark.asyncio
async def test_gemini_client_rate_limiter_sleeps_and_retries_on_rpm() -> None:
    mock_limiter = AsyncMock(spec=GeminiRateLimiter)
    # First call blocked with 1.0s wait, second call allowed
    mock_limiter.acquire.side_effect = [
        RateLimitResult(allowed=False, reason="RPM_EXHAUSTED", retry_after_s=0.01),
        RateLimitResult(allowed=True, reason="OK", retry_after_s=0.0),
    ]

    client = GeminiClient(
        api_key="fake-key",
        rate_limiter=mock_limiter,
    )

    mock_response = MagicMock()
    mock_response.text = '{"message": "delayed_ok", "code": 1}'
    mock_genai = MagicMock()
    mock_genai.models.generate_content.return_value = mock_response
    client._client = mock_genai

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        res = await client.generate_structured("prompt", schema=DummyOutput)
        mock_sleep.assert_awaited_once_with(0.01)
        assert res.message == "delayed_ok"
