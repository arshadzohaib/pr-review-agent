"""Unit tests for Phase 6 agent schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent.schemas import PatchProposal, ReproductionTestPlan, ReviewFinding, ReviewResponse


def test_review_finding_schema_and_defaults() -> None:
    finding = ReviewFinding(
        file_path="app/calc.py",
        line=42,
        title="ZeroDivisionError in divide",
        explanation="Denominator b is not checked before division.",
    )
    assert finding.severity == "minor"
    assert finding.category == "bug"
    assert finding.confidence == 0.8
    assert finding.reproducible is False


def test_review_finding_confidence_bounds() -> None:
    with pytest.raises(ValidationError):
        ReviewFinding(
            file_path="app/calc.py",
            title="invalid",
            explanation="invalid",
            confidence=1.5,
        )


def test_reproduction_test_plan_schema() -> None:
    plan = ReproductionTestPlan(
        code="def test_zero(): assert divide(1, 0) == 0",
        explanation="Tests divide by zero behavior",
    )
    assert plan.file_name == "test_repro.py"
    assert plan.target_finding_idx == 0


def test_patch_proposal_schema() -> None:
    diff = (
        "--- a/app/calc.py\n+++ b/app/calc.py\n@@ -1 +1 @@\n"
        "-return a / b\n+return a / b if b != 0 else 0\n"
    )
    patch = PatchProposal(
        file_path="app/calc.py",
        unified_diff=diff,
        rationale="Guard against zero denominator",
    )
    assert patch.suggestion_block is None
    assert "return a / b" in patch.unified_diff


def test_review_response_list() -> None:
    f1 = ReviewFinding(file_path="a.py", title="t1", explanation="e1")
    f2 = ReviewFinding(file_path="b.py", title="t2", explanation="e2")
    res = ReviewResponse(findings=[f1, f2], summary="Found 2 issues")
    assert len(res.findings) == 2
    assert res.summary == "Found 2 issues"
