"""Node: generate a minimal unified diff bug-fix patch using Gemini."""

from __future__ import annotations

import logging
from typing import Any

from app.agent.gemini_client import GeminiClientProtocol
from app.agent.prompts.patch_prompts import PATCH_SYSTEM_INSTRUCTION, build_patch_prompt
from app.agent.schemas import PatchProposal
from app.agent.state import AgentState

logger = logging.getLogger(__name__)


async def generate_patch_node(
    state: AgentState,
    gemini_client: GeminiClientProtocol,
) -> dict[str, Any]:
    """Generate a surgical unified diff patch to fix the confirmed bug."""
    finding = state.get("target_finding")
    test_plan = state.get("reproduction_test")
    if finding is None or test_plan is None:
        return {
            "status": "clean",
            "proposed_patch": None,
        }

    files = state.get("files", {})
    file_content = files.get(finding.file_path, "")

    # Collect failure logs from latest sandbox execution
    sb_res = state.get("sandbox_result")
    repro_logs = ""
    if sb_res is not None:
        repro_logs = sb_res.stderr_tail or sb_res.stdout_tail or sb_res.stdout or sb_res.stderr

    # Collect retry feedback if this is a subsequent attempt
    retry_feedback: str | None = None
    if state.get("retry_count", 0) > 0 and sb_res is not None:
        retry_feedback = (
            f"Exit code: {sb_res.exit_code}\nOutcome: {sb_res.outcome}\nLogs:\n{repro_logs}"
        )

    prompt = build_patch_prompt(
        finding=finding,
        test_plan=test_plan,
        repro_logs=repro_logs,
        original_file_content=file_content,
        retry_feedback=retry_feedback,
    )

    try:
        patch_proposal: PatchProposal = await gemini_client.generate_structured(
            prompt=prompt,
            schema=PatchProposal,
            system_instruction=PATCH_SYSTEM_INSTRUCTION,
        )
    except Exception as exc:
        logger.error("Patch generation failed: %s", exc)
        return {
            "status": "error",
            "error_reason": f"Patch generation failed: {exc}",
            "proposed_patch": None,
            "llm_calls_used": state.get("llm_calls_used", 0) + 1,
        }

    llm_calls = state.get("llm_calls_used", 0) + 1
    return {
        "proposed_patch": patch_proposal,
        "status": "patch_generated",
        "llm_calls_used": llm_calls,
    }
