"""Node: generate a focused pytest reproduction test for the target finding."""

from __future__ import annotations

import logging
from typing import Any

from app.agent.gemini_client import GeminiClientProtocol
from app.agent.prompts.repro_prompts import REPRO_SYSTEM_INSTRUCTION, build_repro_prompt
from app.agent.schemas import ReproductionTestPlan
from app.agent.state import AgentState

logger = logging.getLogger(__name__)


async def generate_reproduction_node(
    state: AgentState,
    gemini_client: GeminiClientProtocol,
) -> dict[str, Any]:
    """Generate self-contained pytest reproduction code."""
    finding = state.get("target_finding")
    if finding is None:
        return {
            "status": "clean",
            "reproduction_test": None,
        }

    files = state.get("files", {})
    file_content = files.get(finding.file_path)

    prompt = build_repro_prompt(finding, file_content)

    try:
        test_plan: ReproductionTestPlan = await gemini_client.generate_structured(
            prompt=prompt,
            schema=ReproductionTestPlan,
            system_instruction=REPRO_SYSTEM_INSTRUCTION,
        )
    except Exception as exc:
        logger.error("Reproduction test generation failed: %s", exc)
        return {
            "status": "error",
            "error_reason": f"Reproduction generation failed: {exc}",
            "reproduction_test": None,
            "llm_calls_used": state.get("llm_calls_used", 0) + 1,
        }

    llm_calls = state.get("llm_calls_used", 0) + 1
    return {
        "reproduction_test": test_plan,
        "status": "reproduction_ready",
        "llm_calls_used": llm_calls,
    }
