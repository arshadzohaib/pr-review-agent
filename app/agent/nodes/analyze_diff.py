"""Node: analyze diff and AST context to identify code review findings."""

from __future__ import annotations

import logging
from typing import Any

from app.agent.gemini_client import GeminiClientProtocol
from app.agent.prompts.review_prompts import REVIEW_SYSTEM_INSTRUCTION, build_review_prompt
from app.agent.schemas import ReviewFinding, ReviewResponse
from app.agent.state import AgentState

logger = logging.getLogger(__name__)

SEVERITY_WEIGHTS: dict[str, float] = {
    "critical": 4.0,
    "major": 3.0,
    "minor": 2.0,
    "info": 1.0,
}


def _score_finding(f: ReviewFinding) -> float:
    return SEVERITY_WEIGHTS.get(f.severity, 1.0) * f.confidence


async def analyze_diff_node(
    state: AgentState,
    gemini_client: GeminiClientProtocol,
) -> dict[str, Any]:
    """Inspect PR diff and AST context to detect bugs and vulnerabilities."""
    diff_hunks = state.get("diff_hunks", [])
    ast_context = state.get("ast_context", [])

    prompt = build_review_prompt(diff_hunks, ast_context)

    try:
        review_res: ReviewResponse = await gemini_client.generate_structured(
            prompt=prompt,
            schema=ReviewResponse,
            system_instruction=REVIEW_SYSTEM_INSTRUCTION,
        )
        findings = review_res.findings
    except Exception as exc:
        logger.error("Diff analysis failed: %s", exc)
        return {
            "findings": [],
            "status": "error",
            "error_reason": f"Analysis failed: {exc}",
            "target_finding": None,
            "llm_calls_used": state.get("llm_calls_used", 0) + 1,
        }

    # Filter for reproducible bugs and security flaws
    reproducible_bugs = [
        f
        for f in findings
        if f.category in ("bug", "security") and f.reproducible and f.confidence >= 0.7
    ]

    llm_calls = state.get("llm_calls_used", 0) + 1

    if not reproducible_bugs:
        logger.info("No reproducible bugs identified. Exiting review cleanly.")
        return {
            "findings": findings,
            "status": "clean",
            "target_finding": None,
            "llm_calls_used": llm_calls,
        }

    # Select the highest-impact bug (severity * confidence)
    top_bug = max(reproducible_bugs, key=_score_finding)
    logger.info("Selected top bug for reproduction: %s (%s)", top_bug.title, top_bug.file_path)

    return {
        "findings": findings,
        "status": "reproducing",
        "target_finding": top_bug,
        "llm_calls_used": llm_calls,
    }
