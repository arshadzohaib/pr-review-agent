"""Data models and outcome classification for sandbox container execution."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict

DEFAULT_LOG_TAIL_CHARS: Final[int] = 4000


class ExecutionOutcome(StrEnum):
    """Normalized categorical outcome of untrusted code execution."""

    PASSED = "passed"
    TEST_FAILED = "test_failed"
    PATCH_FAILED = "patch_failed"
    ENV_ERROR = "env_error"
    TIMEOUT = "timeout"
    OOM = "oom"
    DOCKER_UNAVAILABLE = "docker_unavailable"


class SandboxResult(BaseModel):
    """Structured execution report returned from an isolated container run."""

    model_config = ConfigDict(frozen=True)

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_ms: float = 0.0
    test_passed: bool = False
    outcome: ExecutionOutcome
    docker_available: bool = True
    stdout_tail: str = ""
    stderr_tail: str = ""


def _tail(text: str, max_chars: int = DEFAULT_LOG_TAIL_CHARS) -> str:
    """Return the last max_chars characters of text."""
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def classify_outcome(
    exit_code: int,
    stdout: str,
    stderr: str,
    timed_out: bool,
    docker_available: bool = True,
) -> ExecutionOutcome:
    """Deterministically categorize execution result from container logs and status."""
    if not docker_available:
        return ExecutionOutcome.DOCKER_UNAVAILABLE
    if timed_out:
        return ExecutionOutcome.TIMEOUT
    if exit_code == 137:
        return ExecutionOutcome.OOM

    combined = f"{stdout}\n{stderr}"

    # Missing third-party dependency / import failure
    if "ModuleNotFoundError:" in combined or "ImportError:" in combined:
        return ExecutionOutcome.ENV_ERROR

    # Git apply patch conflict or corruption
    if (
        "error: patch failed:" in combined
        or "error: corrupt patch" in combined
        or "error: unrecognized input" in combined
    ):
        return ExecutionOutcome.PATCH_FAILED

    if exit_code == 0:
        return ExecutionOutcome.PASSED
    elif exit_code in (1, 2, 4, 5):  # pytest standard failure codes
        return ExecutionOutcome.TEST_FAILED
    else:
        return ExecutionOutcome.ENV_ERROR


def make_sandbox_result(
    exit_code: int,
    stdout: str,
    stderr: str,
    duration_ms: float,
    timed_out: bool = False,
    docker_available: bool = True,
) -> SandboxResult:
    """Construct an immutable SandboxResult with computed tails and outcome."""
    outcome = classify_outcome(
        exit_code, stdout, stderr, timed_out, docker_available=docker_available
    )
    return SandboxResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        duration_ms=duration_ms,
        test_passed=(outcome == ExecutionOutcome.PASSED),
        outcome=outcome,
        docker_available=docker_available,
        stdout_tail=_tail(stdout),
        stderr_tail=_tail(stderr),
    )
