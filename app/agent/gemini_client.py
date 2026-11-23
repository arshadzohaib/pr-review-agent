"""Gemini client wrapper with structured Pydantic outputs and Lua rate limiting."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel

from app.core.rate_limiter import GeminiRateLimiter, estimate_tokens

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class GeminiClientProtocol(Protocol):
    """Protocol for structured Gemini text/code generation."""

    async def generate_structured(
        self,
        prompt: str,
        schema: type[T],
        system_instruction: str | None = None,
    ) -> T:
        """Generate structured Pydantic model response conforming to schema."""
        ...


class GeminiClient:
    """Production Gemini client enforcing atomic Redis Lua rate limits."""

    def __init__(
        self,
        api_key: str,
        model_name: str = "gemini-2.5-flash",
        rate_limiter: GeminiRateLimiter | None = None,
        max_wait_s: int = 90,
    ) -> None:
        self.api_key = api_key
        self.model_name = model_name
        self.rate_limiter = rate_limiter
        self.max_wait_s = max_wait_s
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def _sync_generate(
        self,
        prompt: str,
        schema: type[T],
        system_instruction: str | None = None,
    ) -> T:
        """Synchronous generation using google.genai Client."""
        client = self._get_client()
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            system_instruction=system_instruction,
            temperature=0.2,
        )
        response = client.models.generate_content(
            model=self.model_name,
            contents=prompt,
            config=config,
        )

        if response.text:
            return schema.model_validate_json(response.text)

        if hasattr(response, "parsed") and response.parsed is not None:
            if isinstance(response.parsed, schema):
                return response.parsed
            return schema.model_validate(response.parsed)

        raise ValueError("Empty or unparsable response received from Gemini API.")

    async def generate_structured(
        self,
        prompt: str,
        schema: type[T],
        system_instruction: str | None = None,
    ) -> T:
        """Asynchronously generate structured output while enforcing free-tier rate limits."""
        # 1. Enforce atomic Lua rate limiter if configured
        if self.rate_limiter is not None:
            est_tokens = estimate_tokens(prompt)
            limit_res = await self.rate_limiter.acquire(
                model=self.model_name,
                estimated_tokens=est_tokens,
            )

            if not limit_res.allowed:
                if limit_res.reason in ("RPM_EXHAUSTED", "TPM_EXHAUSTED", "BACKOFF_ACTIVE"):
                    if limit_res.retry_after_s <= self.max_wait_s:
                        logger.warning(
                            "Gemini %s reached. Sleeping for %.1fs...",
                            limit_res.reason,
                            limit_res.retry_after_s,
                        )
                        await asyncio.sleep(limit_res.retry_after_s)
                        # Try acquisition once more after waiting
                        recheck = await self.rate_limiter.acquire(
                            model=self.model_name,
                            estimated_tokens=est_tokens,
                        )
                        if not recheck.allowed:
                            raise RuntimeError(
                                f"Gemini quota still blocked after waiting: {recheck.reason}"
                            )
                    else:
                        raise RuntimeError(
                            f"Gemini rate limit exceeded: {limit_res.reason} "
                            f"(wait time {limit_res.retry_after_s}s exceeds max {self.max_wait_s}s)"
                        )
                elif limit_res.reason == "RPD_EXHAUSTED":
                    raise RuntimeError(
                        f"Gemini daily quota (RPD) exhausted. Resets in {limit_res.retry_after_s}s."
                    )

        # 2. Invoke generation in worker thread
        try:
            return await asyncio.to_thread(
                self._sync_generate,
                prompt,
                schema,
                system_instruction,
            )
        except Exception as exc:
            logger.error("Gemini structured generation failed: %s", exc)
            # If 429 backoff was encountered, record backoff in rate limiter
            if "429" in str(exc) and self.rate_limiter is not None:
                await self.rate_limiter.record_backoff(model=self.model_name, delay_seconds=30.0)
            raise
