"""Node: verify proposed patch in Docker sandbox by applying it and re-testing."""

from __future__ import annotations

import logging
from typing import Any

from app.agent.state import AgentState
from app.sandbox.models import ExecutionOutcome, SandboxResult
from app.sandbox.runner import run_in_sandbox
from app.sandbox.workspace import sandbox_workspace

logger = logging.getLogger(__name__)


async def verify_patch_node(
    state: AgentState,
    docker_client: Any = None,
) -> dict[str, Any]:
    """Apply the proposed patch inside an isolated sandbox and re-run reproduction test."""
    patch_proposal = state.get("proposed_patch")
    test_plan = state.get("reproduction_test")
    if patch_proposal is None or test_plan is None:
        return {
            "verified": False,
            "status": "clean",
        }

    files = state.get("files", {})

    test_cmd = "git apply patch.diff && pytest -o cache_dir=/tmp/.pytest_cache -q test_repro.py"
    with sandbox_workspace(
        files,
        test_code=test_plan.code,
        patch_diff=patch_proposal.unified_diff,
    ) as ws:
        result: SandboxResult = run_in_sandbox(
            workspace_dir=ws,
            command=test_cmd,
            timeout_s=30.0,
            client=docker_client,
        )

    # 1. Success: patch applied cleanly and reproduction test passed!
    if result.outcome == ExecutionOutcome.PASSED and result.exit_code == 0:
        logger.info("Patch verified successfully! Reproduction test passed.")
        return {
            "sandbox_result": result,
            "verified": True,
            "status": "patched",
        }

    # 2. Failure: either patch application failed or test still failed
    retry_count = state.get("retry_count", 0) + 1
    max_retries = state.get("max_retries", 2)

    logger.warning(
        "Patch verification failed (outcome=%s, exit_code=%d). Retry %d of %d.",
        result.outcome,
        result.exit_code,
        retry_count,
        max_retries,
    )

    if retry_count >= max_retries:
        logger.warning(
            "Max retries (%d) reached without verified patch. Downgrading to review-only.",
            max_retries,
        )
        return {
            "sandbox_result": result,
            "verified": False,
            "retry_count": retry_count,
            "status": "max_retries_exceeded",
            "error_reason": f"Patch verification failed after {retry_count} attempts",
        }

    return {
        "sandbox_result": result,
        "verified": False,
        "retry_count": retry_count,
        "status": "retry_patch",
    }
