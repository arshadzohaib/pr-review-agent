"""Node: execute reproduction test in isolated Docker sandbox to confirm failure."""

from __future__ import annotations

import logging
from typing import Any

from app.agent.state import AgentState
from app.sandbox.models import ExecutionOutcome, SandboxResult
from app.sandbox.runner import run_in_sandbox
from app.sandbox.workspace import sandbox_workspace

logger = logging.getLogger(__name__)


async def run_reproduction_node(
    state: AgentState,
    docker_client: Any = None,
) -> dict[str, Any]:
    """Run reproduction test in Docker sandbox to verify it genuinely fails."""
    test_plan = state.get("reproduction_test")
    if test_plan is None:
        return {"status": "clean"}

    files = state.get("files", {})

    with sandbox_workspace(files, test_code=test_plan.code) as ws:
        result: SandboxResult = run_in_sandbox(
            workspace_dir=ws,
            command="pytest -o cache_dir=/tmp/.pytest_cache -q test_repro.py",
            timeout_s=30.0,
            client=docker_client,
        )

    # 1. Fallback on Docker unavailable or missing environment dependencies
    if not result.docker_available or result.outcome in (
        ExecutionOutcome.DOCKER_UNAVAILABLE,
        ExecutionOutcome.ENV_ERROR,
    ):
        logger.warning(
            "Docker sandbox unavailable or environment error encountered (%s). "
            "Downgrading to static review-only.",
            result.outcome,
        )
        return {
            "sandbox_result": result,
            "status": "env_fallback",
            "error_reason": f"Sandbox downgraded: {result.outcome.value}",
        }

    # 2. False positive discard: test PASSED on baseline (bug not confirmed)
    if result.outcome == ExecutionOutcome.PASSED:
        logger.info(
            "Reproduction test PASSED on baseline code! Discarding finding as false positive."
        )
        return {
            "sandbox_result": result,
            "status": "discarded",
            "error_reason": (
                "Bug not confirmed: reproduction test passed on baseline (false positive)"
            ),
        }

    # 3. Bug confirmed failing: pytest failed as expected
    if result.outcome == ExecutionOutcome.TEST_FAILED:
        logger.info("Bug confirmed: reproduction test failed as expected.")
        return {
            "sandbox_result": result,
            "status": "reproduced",
        }

    # 4. Other failures (timeout, OOM)
    logger.warning("Reproduction execution error: %s", result.outcome)
    return {
        "sandbox_result": result,
        "status": "env_fallback",
        "error_reason": f"Reproduction halted: {result.outcome.value}",
    }
