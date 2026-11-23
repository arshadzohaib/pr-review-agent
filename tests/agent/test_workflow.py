"""Unit tests for LangGraph state machine workflow covering all 5 core execution branches."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.graph import create_review_graph
from app.agent.schemas import PatchProposal, ReproductionTestPlan, ReviewFinding, ReviewResponse
from app.agent.state import AgentState
from app.sandbox.models import ExecutionOutcome, SandboxResult


@pytest.fixture
def mock_gemini() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def base_state(tmp_path: Path) -> AgentState:
    diff_text = "@@ -1 +1 @@\n-def div(a, b): return a / b\n"
    return {
        "repo_path": str(tmp_path),
        "diff_hunks": [{"file_path": "calc.py", "hunk": diff_text}],
        "ast_context": [],
        "findings": [],
        "files": {"calc.py": "def div(a, b):\n    return a / b\n"},
        "status": "running",
        "retry_count": 0,
        "max_retries": 2,
        "llm_calls_used": 0,
    }


@pytest.mark.asyncio
async def test_workflow_clean_pr_early_exit(
    mock_gemini: AsyncMock, base_state: AgentState
) -> None:
    # 1. Clean PR scenario: Gemini returns no findings
    mock_gemini.generate_structured.return_value = ReviewResponse(findings=[], summary="LGTM")

    graph = create_review_graph(gemini_client=mock_gemini)
    final_state = await graph.ainvoke(base_state)

    assert final_state["status"] == "clean"
    assert final_state["target_finding"] is None
    assert final_state.get("reproduction_test") is None
    assert final_state.get("proposed_patch") is None
    # Only 1 LLM call used (analyze_diff)
    assert final_state["llm_calls_used"] == 1


@pytest.mark.asyncio
async def test_workflow_confirmed_bug_verified_patch(
    mock_gemini: AsyncMock, base_state: AgentState
) -> None:
    # 2. Confirmed bug -> verified patch loop
    finding = ReviewFinding(
        file_path="calc.py",
        line=2,
        severity="critical",
        category="bug",
        confidence=0.95,
        title="ZeroDivisionError",
        explanation="div by zero not handled",
        reproducible=True,
    )
    test_plan = ReproductionTestPlan(
        code="def test_zero(): from calc import div; assert div(1, 0) == 0",
    )
    diff_text = (
        "--- a/calc.py\n+++ b/calc.py\n@@ -2 +2 @@\n"
        "-    return a / b\n+    return a / b if b != 0 else 0\n"
    )
    patch_proposal = PatchProposal(
        file_path="calc.py",
        unified_diff=diff_text,
        rationale="Guard against zero denominator",
    )

    # Gemini responses sequence: analyze -> repro -> patch
    mock_gemini.generate_structured.side_effect = [
        ReviewResponse(findings=[finding]),
        test_plan,
        patch_proposal,
    ]

    # Sandbox sequence:
    # run 1 (baseline reproduction): fails with exit code 1 (bug confirmed!)
    # run 2 (verify patch): passes with exit code 0 (patch verified!)
    sb_repro_fail = SandboxResult(
        exit_code=1,
        stdout="FAILED test_repro.py::test_zero",
        stderr="ZeroDivisionError",
        outcome=ExecutionOutcome.TEST_FAILED,
        test_passed=False,
    )
    sb_patch_pass = SandboxResult(
        exit_code=0,
        stdout="1 passed in 0.05s",
        stderr="",
        outcome=ExecutionOutcome.PASSED,
        test_passed=True,
    )

    with (
        patch("app.agent.nodes.run_reproduction.run_in_sandbox", return_value=sb_repro_fail),
        patch("app.agent.nodes.verify_patch.run_in_sandbox", return_value=sb_patch_pass),
    ):
        graph = create_review_graph(gemini_client=mock_gemini)
        final_state = await graph.ainvoke(base_state)

    assert final_state["status"] == "patched"
    assert final_state["verified"] is True
    assert final_state["proposed_patch"] is not None
    assert final_state["retry_count"] == 0
    assert final_state["llm_calls_used"] == 3


@pytest.mark.asyncio
async def test_workflow_false_positive_bug_discard(
    mock_gemini: AsyncMock, base_state: AgentState
) -> None:
    # 3. False positive scenario: reproduction test PASSES on baseline code
    finding = ReviewFinding(
        file_path="calc.py",
        severity="major",
        category="bug",
        confidence=0.85,
        title="Spurious bug",
        explanation="Not actually buggy",
        reproducible=True,
    )
    test_plan = ReproductionTestPlan(code="def test_ok(): assert True")

    mock_gemini.generate_structured.side_effect = [
        ReviewResponse(findings=[finding]),
        test_plan,
    ]

    sb_baseline_pass = SandboxResult(
        exit_code=0,
        stdout="1 passed",
        stderr="",
        outcome=ExecutionOutcome.PASSED,
        test_passed=True,
    )

    with patch("app.agent.nodes.run_reproduction.run_in_sandbox", return_value=sb_baseline_pass):
        graph = create_review_graph(gemini_client=mock_gemini)
        final_state = await graph.ainvoke(base_state)

    assert final_state["status"] == "discarded"
    err = final_state.get("error_reason", "").lower()
    assert "false positive" in err or "not confirmed" in err
    # No patch attempted
    assert final_state.get("proposed_patch") is None


@pytest.mark.asyncio
async def test_workflow_missing_dependency_fallback(
    mock_gemini: AsyncMock, base_state: AgentState
) -> None:
    # 4. Missing dependency scenario: ModuleNotFoundError in sandbox
    finding = ReviewFinding(
        file_path="calc.py",
        severity="major",
        category="bug",
        confidence=0.9,
        title="Import flaw",
        explanation="Needs third party package",
        reproducible=True,
    )
    test_plan = ReproductionTestPlan(code="import fastapi\ndef test_api(): pass")

    mock_gemini.generate_structured.side_effect = [
        ReviewResponse(findings=[finding]),
        test_plan,
    ]

    sb_env_err = SandboxResult(
        exit_code=1,
        stdout="",
        stderr="ModuleNotFoundError: No module named 'fastapi'",
        outcome=ExecutionOutcome.ENV_ERROR,
        test_passed=False,
    )

    with patch("app.agent.nodes.run_reproduction.run_in_sandbox", return_value=sb_env_err):
        graph = create_review_graph(gemini_client=mock_gemini)
        final_state = await graph.ainvoke(base_state)

    assert final_state["status"] == "env_fallback"
    assert "downgraded" in final_state.get("error_reason", "").lower()
    assert final_state.get("proposed_patch") is None


@pytest.mark.asyncio
async def test_workflow_max_retries_termination(
    mock_gemini: AsyncMock, base_state: AgentState
) -> None:
    # 5. Max retries guardrail: patch fails verification 2 times and terminates cleanly
    finding = ReviewFinding(
        file_path="calc.py",
        severity="critical",
        category="bug",
        confidence=0.95,
        title="Tough Bug",
        explanation="Hard to fix",
        reproducible=True,
    )
    test_plan = ReproductionTestPlan(code="def test_repro(): assert False")
    patch1 = PatchProposal(file_path="calc.py", unified_diff="diff 1", rationale="r1")
    patch2 = PatchProposal(file_path="calc.py", unified_diff="diff 2", rationale="r2")

    # Sequence: analyze -> repro -> patch1 -> patch2
    mock_gemini.generate_structured.side_effect = [
        ReviewResponse(findings=[finding]),
        test_plan,
        patch1,
        patch2,
    ]

    sb_repro_fail = SandboxResult(
        exit_code=1,
        stdout="FAILED",
        stderr="assert False",
        outcome=ExecutionOutcome.TEST_FAILED,
        test_passed=False,
    )
    sb_patch_fail = SandboxResult(
        exit_code=1,
        stdout="FAILED",
        stderr="Patch still failed test",
        outcome=ExecutionOutcome.TEST_FAILED,
        test_passed=False,
    )

    with (
        patch("app.agent.nodes.run_reproduction.run_in_sandbox", return_value=sb_repro_fail),
        patch("app.agent.nodes.verify_patch.run_in_sandbox", return_value=sb_patch_fail),
    ):
        graph = create_review_graph(gemini_client=mock_gemini)
        final_state = await graph.ainvoke(base_state)

    assert final_state["status"] == "max_retries_exceeded"
    assert final_state["verified"] is False
    assert final_state["retry_count"] == 2
    err = final_state.get("error_reason", "").lower()
    assert "max retries" in err or "failed after 2 attempts" in err
