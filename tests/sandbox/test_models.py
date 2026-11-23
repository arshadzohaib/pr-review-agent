"""Unit tests for sandbox data models and outcome classification."""

from __future__ import annotations

import pytest

from app.sandbox.models import ExecutionOutcome, classify_outcome, make_sandbox_result


@pytest.mark.parametrize(
    ("exit_code", "stdout", "stderr", "timed_out", "docker_available", "expected"),
    [
        (0, "1 passed in 0.05s", "", False, True, ExecutionOutcome.PASSED),
        (1, "1 failed, 0 passed", "", False, True, ExecutionOutcome.TEST_FAILED),
        (2, "usage error", "", False, True, ExecutionOutcome.TEST_FAILED),
        (137, "", "Killed", False, True, ExecutionOutcome.OOM),
        (124, "", "timeout", True, True, ExecutionOutcome.TIMEOUT),
        (-1, "", "docker down", False, False, ExecutionOutcome.DOCKER_UNAVAILABLE),
        (
            1,
            "",
            "ModuleNotFoundError: No module named 'fastapi'",
            False,
            True,
            ExecutionOutcome.ENV_ERROR,
        ),
        (1, "", "error: patch failed: app/calc.py:5", False, True, ExecutionOutcome.PATCH_FAILED),
    ],
)
def test_classify_outcome(
    exit_code: int,
    stdout: str,
    stderr: str,
    timed_out: bool,
    docker_available: bool,
    expected: ExecutionOutcome,
) -> None:
    res = classify_outcome(
        exit_code, stdout, stderr, timed_out, docker_available=docker_available
    )
    assert res == expected


def test_make_sandbox_result_tail_and_passed_flag() -> None:
    long_stdout = "x" * 6000
    res = make_sandbox_result(exit_code=0, stdout=long_stdout, stderr="", duration_ms=120.0)

    assert res.test_passed is True
    assert res.outcome == ExecutionOutcome.PASSED
    assert len(res.stdout_tail) == 4000
    assert res.duration_ms == 120.0
