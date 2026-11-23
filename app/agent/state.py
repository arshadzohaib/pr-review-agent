"""Typed AgentState schema for LangGraph review-test-patch state machine."""

from __future__ import annotations

from typing import Any, TypedDict

from app.agent.schemas import PatchProposal, ReproductionTestPlan, ReviewFinding
from app.sandbox.models import SandboxResult


class AgentState(TypedDict, total=False):
    """Typed state dictionary passed across LangGraph nodes.

    Includes all fields required by Phase 6 specification:
    - repo_path: Local repository root or workspace path.
    - diff_hunks: Extracted unified diff hunks or DiffChunk objects.
    - ast_context: List of ASTContext payloads.
    - findings: List of ReviewFinding objects.
    - reproduction_test: ReproductionTestPlan if generated.
    - sandbox_result: SandboxResult from the latest Docker run.
    - proposed_patch: PatchProposal if generated.
    - verified: True if reproduction test passes under patched workspace.
    - retry_count: Number of patch retry iterations (capped at max_retries).
    - error_reason: Explanation if downgraded, failed, or skipped.
    """

    repo_path: str
    diff_hunks: list[Any]
    ast_context: list[Any]
    findings: list[ReviewFinding]
    target_finding: ReviewFinding | None
    reproduction_test: ReproductionTestPlan | None
    sandbox_result: SandboxResult | None
    proposed_patch: PatchProposal | None
    verified: bool
    retry_count: int
    error_reason: str | None
    files: dict[str, str]
    status: str
    max_retries: int
    llm_calls_used: int
