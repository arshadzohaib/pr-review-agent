"""Redis SHA-based diff caching and deduplication helpers.

See ARCHITECTURE.md §3.2 for specifications:
- Delivery dedupe: delivery:{X-GitHub-Delivery} (24h)
- PR-head done: done:{repo}:{pr}:{head_sha} (30d)
- Review chunk cache: rv:{PROMPT_VERSION}:{model}:{chunk_sha} (7d)
- AST symbols: ast:{lang}:{blob_sha} (7d)
- Sandbox result: sbx:{cache_key} (1d)
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Final, cast

from redis.asyncio import Redis

DEFAULT_DELIVERY_TTL_S: Final[int] = 86_400  # 24 hours
DEFAULT_PR_DONE_TTL_S: Final[int] = 30 * 86_400  # 30 days
DEFAULT_REVIEW_CACHE_TTL_S: Final[int] = 7 * 86_400  # 7 days
DEFAULT_AST_TTL_S: Final[int] = 7 * 86_400  # 7 days
DEFAULT_SANDBOX_TTL_S: Final[int] = 86_400  # 24 hours

_WS_RE: Final[re.Pattern[str]] = re.compile(r"[ \t]+")


def normalize_function_source(source: str, lang: str = "python") -> str:
    """Normalize function source code for path-independent caching.

    Strips line comments, removes trailing whitespace, collapses multiple internal spaces/tabs,
    and drops blank lines. Leading indentation is preserved because it is semantic in Python.
    """
    lines: list[str] = []
    for raw_line in source.splitlines():
        if lang.lower() == "python":
            line_without_comment = re.sub(r"#.*$", "", raw_line)
        else:
            line_without_comment = re.sub(r"//.*$", "", raw_line)

        stripped = line_without_comment.rstrip()
        if not stripped.strip():
            continue

        indent_len = len(stripped) - len(stripped.lstrip())
        indent = stripped[:indent_len]
        body = _WS_RE.sub(" ", stripped[indent_len:].strip())
        lines.append(f"{indent}{body}")

    return "\n".join(lines)


def compute_chunk_sha(norm_func_src: str, ctx_hash: str, lang: str = "python") -> str:
    """Compute deterministic SHA-256 for a function chunk + caller context + language.

    The file path is intentionally omitted so moved/renamed code reuses cache hits.
    """
    hasher = hashlib.sha256()
    hasher.update(norm_func_src.encode("utf-8"))
    hasher.update(b"\x00")
    hasher.update(ctx_hash.encode("utf-8"))
    hasher.update(b"\x00")
    hasher.update(lang.lower().encode("utf-8"))
    return hasher.hexdigest()


def compute_sandbox_key(image: str, tree_sha: str, patch: str, test: str) -> str:
    """Compute deterministic SHA-256 for sandbox execution caching."""
    hasher = hashlib.sha256()
    for item in (image, tree_sha, patch, test):
        hasher.update(item.encode("utf-8"))
        hasher.update(b"\x00")
    return hasher.hexdigest()


class ReviewCache:
    """Asynchronous Redis-backed cache for PR review pipelines."""

    def __init__(
        self, client: Redis, default_review_ttl_s: int = DEFAULT_REVIEW_CACHE_TTL_S
    ) -> None:
        self.client = client
        self.default_review_ttl_s = default_review_ttl_s

    # --- Delivery Deduplication ---
    async def check_and_set_delivery(
        self, delivery_id: str, ttl_s: int = DEFAULT_DELIVERY_TTL_S
    ) -> bool:
        """Atomic check-and-set for GitHub webhook delivery GUIDs.

        Returns True if this delivery was new and successfully recorded, False if already seen.
        """
        if not delivery_id:
            return True
        key = f"delivery:{delivery_id}"
        result = await self.client.set(key, "1", nx=True, ex=ttl_s)
        return bool(result)

    # --- PR Head Done ---
    async def is_pr_head_done(self, repo: str, pr: int, head_sha: str) -> str | None:
        """Check if this commit on this PR was already reviewed."""
        key = f"done:{repo}:{pr}:{head_sha}"
        val = await self.client.get(key)
        return str(val) if val is not None else None

    async def mark_pr_head_done(
        self,
        repo: str,
        pr: int,
        head_sha: str,
        review_id: str,
        ttl_s: int = DEFAULT_PR_DONE_TTL_S,
    ) -> None:
        """Mark this commit on this PR as completed."""
        key = f"done:{repo}:{pr}:{head_sha}"
        await self.client.set(key, review_id, ex=ttl_s)

    # --- Review Chunk Cache ---
    async def get_review_chunk(
        self, chunk_sha: str, prompt_version: int, model: str
    ) -> list[dict[str, Any]] | None:
        """Fetch cached LLM findings for a function chunk."""
        key = f"rv:{prompt_version}:{model}:{chunk_sha}"
        val = await self.client.get(key)
        if val is None:
            return None
        try:
            data = json.loads(val)
            if isinstance(data, list):
                return cast(list[dict[str, Any]], data)
            return None
        except Exception:
            return None

    async def set_review_chunk(
        self,
        chunk_sha: str,
        prompt_version: int,
        model: str,
        findings: list[Any],
        ttl_s: int | None = None,
    ) -> None:
        """Store findings for a function chunk in Redis."""
        key = f"rv:{prompt_version}:{model}:{chunk_sha}"
        ttl = ttl_s if ttl_s is not None else self.default_review_ttl_s
        serializable = [
            f.model_dump() if hasattr(f, "model_dump") else f for f in findings
        ]
        payload = json.dumps(serializable, ensure_ascii=False)
        await self.client.set(key, payload, ex=ttl)

    # --- AST Symbols Cache ---
    async def get_ast_symbols(self, lang: str, blob_sha: str) -> dict[str, Any] | None:
        """Fetch cached parsed AST symbols for a file blob."""
        key = f"ast:{lang.lower()}:{blob_sha}"
        val = await self.client.get(key)
        if val is None:
            return None
        try:
            data = json.loads(val)
            if isinstance(data, dict):
                return cast(dict[str, Any], data)
            return None
        except Exception:
            return None

    async def set_ast_symbols(
        self, lang: str, blob_sha: str, symbols: dict[str, Any], ttl_s: int = DEFAULT_AST_TTL_S
    ) -> None:
        """Store parsed AST symbols for a file blob in Redis."""
        key = f"ast:{lang.lower()}:{blob_sha}"
        payload = json.dumps(symbols, ensure_ascii=False)
        await self.client.set(key, payload, ex=ttl_s)

    # --- Sandbox Result Cache ---
    async def get_sandbox_result(self, cache_key: str) -> dict[str, Any] | None:
        """Fetch cached sandbox execution result."""
        key = f"sbx:{cache_key}"
        val = await self.client.get(key)
        if val is None:
            return None
        try:
            data = json.loads(val)
            if isinstance(data, dict):
                return cast(dict[str, Any], data)
            return None
        except Exception:
            return None

    async def set_sandbox_result(
        self, cache_key: str, result: dict[str, Any], ttl_s: int = DEFAULT_SANDBOX_TTL_S
    ) -> None:
        """Store sandbox execution result in Redis."""
        key = f"sbx:{cache_key}"
        payload = json.dumps(result, ensure_ascii=False)
        await self.client.set(key, payload, ex=ttl_s)
