"""Unit tests for app.core.cache."""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.core.cache import (
    ReviewCache,
    compute_chunk_sha,
    compute_sandbox_key,
    normalize_function_source,
)


def test_normalize_function_source() -> None:
    code = """
def test_calc(a,   b):
    # This is a comment
    result = a + b    # inline comment

    return    result
"""
    norm = normalize_function_source(code, lang="python")
    expected = "\ndef test_calc(a, b):\n    result = a + b\n    return result".strip()
    assert norm == expected


def test_compute_chunk_sha_deterministic() -> None:
    sha1 = compute_chunk_sha("def f(): return 1", "ctx_abc", "python")
    sha2 = compute_chunk_sha("def f(): return 1", "ctx_abc", "python")
    sha3 = compute_chunk_sha("def f(): return 1", "ctx_xyz", "python")

    assert sha1 == sha2
    assert sha1 != sha3
    assert len(sha1) == 64


def test_compute_sandbox_key() -> None:
    k1 = compute_sandbox_key("img:3.11", "tree123", "patchA", "testB")
    k2 = compute_sandbox_key("img:3.11", "tree123", "patchA", "testB")
    assert k1 == k2
    assert len(k1) == 64


@pytest.mark.asyncio
async def test_review_cache_delivery_and_done() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache = ReviewCache(redis)

    # Delivery check
    assert await cache.check_and_set_delivery("deliv-1") is True
    assert await cache.check_and_set_delivery("deliv-1") is False

    # PR head done
    assert await cache.is_pr_head_done("org/repo", 10, "sha-123") is None
    await cache.mark_pr_head_done("org/repo", 10, "sha-123", "rv-99")
    assert await cache.is_pr_head_done("org/repo", 10, "sha-123") == "rv-99"


@pytest.mark.asyncio
async def test_review_cache_chunks_and_ast_and_sandbox() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache = ReviewCache(redis)

    # Review chunk
    findings = [{"line": 10, "title": "Bug found", "severity": "major"}]
    assert await cache.get_review_chunk("chunk-1", 1, "gemini-2.5-flash") is None
    await cache.set_review_chunk("chunk-1", 1, "gemini-2.5-flash", findings)
    cached = await cache.get_review_chunk("chunk-1", 1, "gemini-2.5-flash")
    assert cached == findings

    # AST symbols
    ast_data = {"symbols": ["foo", "bar"]}
    assert await cache.get_ast_symbols("python", "blob-1") is None
    await cache.set_ast_symbols("python", "blob-1", ast_data)
    assert await cache.get_ast_symbols("python", "blob-1") == ast_data

    # Sandbox
    sb_res = {"exit_code": 0, "stdout": "pass"}
    assert await cache.get_sandbox_result("sbx-1") is None
    await cache.set_sandbox_result("sbx-1", sb_res)
    assert await cache.get_sandbox_result("sbx-1") == sb_res
