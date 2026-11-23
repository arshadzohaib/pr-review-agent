"""Unit tests for pure routing functions in graph."""

from __future__ import annotations

import pytest
from langgraph.constants import END

from app.agent.graph import (
    route_after_analyze,
    route_after_generate_patch,
    route_after_generate_repro,
    route_after_run_repro,
    route_after_verify_patch,
)
from app.agent.schemas import PatchProposal, ReproductionTestPlan, ReviewFinding
from app.agent.state import AgentState


def test_route_after_analyze_branches() -> None:
    clean_state: AgentState = {"status": "clean", "target_finding": None}
    assert route_after_analyze(clean_state) == END

    bug = ReviewFinding(file_path="a.py", title="bug", explanation="exp", reproducible=True)
    repro_state: AgentState = {"status": "reproducing", "target_finding": bug}
    assert route_after_analyze(repro_state) == "generate_reproduction"


def test_route_after_generate_repro_branches() -> None:
    plan = ReproductionTestPlan(code="def test_x(): pass")
    ready_state: AgentState = {"status": "reproduction_ready", "reproduction_test": plan}
    assert route_after_generate_repro(ready_state) == "run_reproduction"

    err_state: AgentState = {"status": "error", "reproduction_test": None}
    assert route_after_generate_repro(err_state) == END


def test_route_after_run_repro_branches() -> None:
    assert route_after_run_repro({"status": "reproduced"}) == "generate_patch"
    assert route_after_run_repro({"status": "discarded"}) == END
    assert route_after_run_repro({"status": "env_fallback"}) == END
    assert route_after_run_repro({"status": "clean"}) == END


def test_route_after_generate_patch_branches() -> None:
    patch = PatchProposal(file_path="a.py", unified_diff="...", rationale="fix")
    ok_state: AgentState = {"status": "patch_generated", "proposed_patch": patch}
    assert route_after_generate_patch(ok_state) == "verify_patch"

    err_state: AgentState = {"status": "error", "proposed_patch": None}
    assert route_after_generate_patch(err_state) == END


@pytest.mark.parametrize(
    ("status", "retry_count", "max_retries", "expected"),
    [
        ("patched", 0, 2, END),
        ("retry_patch", 0, 2, "generate_patch"),
        ("retry_patch", 1, 2, "generate_patch"),
        ("retry_patch", 2, 2, END),
        ("max_retries_exceeded", 2, 2, END),
    ],
)
def test_route_after_verify_patch_table(
    status: str, retry_count: int, max_retries: int, expected: str
) -> None:
    state: AgentState = {
        "status": status,
        "retry_count": retry_count,
        "max_retries": max_retries,
    }
    assert route_after_verify_patch(state) == expected
