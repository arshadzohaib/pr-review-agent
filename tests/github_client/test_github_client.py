"""Unit tests for GitHub client, markdown review formatting, and deduplication."""

from __future__ import annotations

import io
import os
import tarfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.schemas import PatchProposal, ReviewFinding
from app.github_client.client import (
    BOT_MARKER_PREFIX,
    PyGithubClient,
    cleanup_pr_workspace,
    format_review_markdown,
)
from app.sandbox.models import ExecutionOutcome, SandboxResult


def test_format_review_markdown_verified_patch() -> None:
    finding = ReviewFinding(
        file_path="calc.py",
        line=10,
        severity="critical",
        category="bug",
        confidence=0.95,
        title="ZeroDivisionError",
        explanation="Divide by zero possible when b=0",
    )
    diff_text = (
        "--- a/calc.py\n+++ b/calc.py\n@@ -10 +10 @@\n"
        "-return a / b\n+return a / b if b != 0 else 0\n"
    )
    patch = PatchProposal(
        file_path="calc.py",
        unified_diff=diff_text,
        rationale="Guard denominator against zero",
        suggestion_block="return a / b if b != 0 else 0",
    )
    sandbox_res = SandboxResult(
        exit_code=0,
        stdout="1 passed",
        stderr="",
        outcome=ExecutionOutcome.PASSED,
        test_passed=True,
        duration_ms=45.2,
    )

    md = format_review_markdown(
        commit_sha="abc1234",
        findings=[finding],
        verified=True,
        proposed_patch=patch,
        sandbox_result=sandbox_res,
        status="patched",
    )

    # Verify marker prefix for deduplication
    assert f"{BOT_MARKER_PREFIX} abc1234 -->" in md
    # Verify alert banner
    assert "Bug Confirmed & Patch Verified" in md
    # Verify table row
    assert "`calc.py`" in md
    assert "🔴 Critical" in md
    assert "ZeroDivisionError" in md
    # Verify diff block
    assert "return a / b if b != 0 else 0" in md
    # Verify diagnostics
    assert "Docker Sandbox Diagnostics" in md
    assert "45.2ms" in md


@pytest.mark.parametrize(
    ("status", "expected_badge"),
    [
        ("clean", "Clean PR"),
        ("env_fallback", "Static Review Only"),
        ("discarded", "False Positive Discarded"),
        ("max_retries_exceeded", "Review Only"),
    ],
)
def test_format_review_markdown_status_badges(status: str, expected_badge: str) -> None:
    md = format_review_markdown(
        commit_sha="commit-sha-999",
        findings=[],
        status=status,
    )
    assert expected_badge in md


@pytest.mark.asyncio
async def test_fetch_pr_workspace_tarball_extraction() -> None:
    client = PyGithubClient(token="fake-token")

    # Create mock in-memory tarball
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w:gz") as tar:
        # File 1: safe file
        f1_data = b"def add(a, b): return a + b\n"
        ti1 = tarfile.TarInfo(name="repo-root/app/calc.py")
        ti1.size = len(f1_data)
        tar.addfile(ti1, io.BytesIO(f1_data))

        # File 2: traversal attempt
        f2_data = b"malicious content"
        ti2 = tarfile.TarInfo(name="repo-root/../../etc/passwd")
        ti2.size = len(f2_data)
        tar.addfile(ti2, io.BytesIO(f2_data))

    tar_bytes = tar_buf.getvalue()
    client.get_repo_archive = AsyncMock(return_value=tar_bytes)  # type: ignore[method-assign]

    ws_dir, files_dict = await client.fetch_pr_workspace("owner/repo", "head-sha")

    try:
        assert os.path.isdir(ws_dir)
        # Verify safe file was extracted
        assert "app/calc.py" in files_dict
        assert "def add" in files_dict["app/calc.py"]
        assert os.path.isfile(os.path.join(ws_dir, "app", "calc.py"))

        # Verify traversal was blocked
        assert not os.path.exists(os.path.join(ws_dir, "..", "etc", "passwd"))
    finally:
        cleanup_pr_workspace(ws_dir)

    assert not os.path.exists(ws_dir)


@pytest.mark.asyncio
async def test_has_bot_reviewed_detection() -> None:
    client = PyGithubClient(token="fake-token")

    mock_pr = MagicMock()
    # Review 1: from someone else
    rev1 = MagicMock()
    rev1.body = "Looks good to me!"
    # Review 2: prior bot review for commit sha 111
    rev2 = MagicMock()
    rev2.body = f"{BOT_MARKER_PREFIX} commit-111 -->\n## Autonomous PR Review"

    mock_pr.get_reviews.return_value = [rev1, rev2]
    mock_repo = MagicMock()
    mock_repo.get_pull.return_value = mock_pr
    client._gh.get_repo = MagicMock(return_value=mock_repo)  # type: ignore[method-assign]

    # Should detect commit-111 as already reviewed
    already_done = await client.has_bot_reviewed("owner/repo", 1, "commit-111")
    assert already_done is True

    # Should detect commit-222 as NOT yet reviewed
    not_done = await client.has_bot_reviewed("owner/repo", 1, "commit-222")
    assert not_done is False
