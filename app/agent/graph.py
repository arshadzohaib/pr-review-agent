"""LangGraph stateful review, reproduction test, and patch verification workflow."""

from __future__ import annotations

from typing import Any

from langgraph.constants import END
from langgraph.graph.state import CompiledStateGraph, StateGraph

from app.agent.gemini_client import GeminiClientProtocol
from app.agent.nodes.analyze_diff import analyze_diff_node
from app.agent.nodes.generate_patch import generate_patch_node
from app.agent.nodes.generate_reproduction import generate_reproduction_node
from app.agent.nodes.run_reproduction import run_reproduction_node
from app.agent.nodes.verify_patch import verify_patch_node
from app.agent.state import AgentState


def route_after_analyze(state: AgentState) -> str:
    """Route after diff analysis: proceed to reproduction if bug found, else end cleanly."""
    if state.get("target_finding") is not None and state.get("status") == "reproducing":
        return "generate_reproduction"
    return str(END)


def route_after_generate_repro(state: AgentState) -> str:
    """Route after generating reproduction test."""
    if state.get("reproduction_test") is not None and state.get("status") == "reproduction_ready":
        return "run_reproduction"
    return str(END)


def route_after_run_repro(state: AgentState) -> str:
    """Route after running reproduction test: proceed to patch if confirmed failing."""
    if state.get("status") == "reproduced":
        return "generate_patch"
    # Handles "clean", "discarded" (false positive), "env_fallback", and errors
    return str(END)


def route_after_generate_patch(state: AgentState) -> str:
    """Route after generating patch."""
    if state.get("proposed_patch") is not None and state.get("status") == "patch_generated":
        return "verify_patch"
    return str(END)


def route_after_verify_patch(state: AgentState) -> str:
    """Route after verifying patch: retry if failed and quota permits, else exit."""
    retry_count = state.get("retry_count", 0)
    max_retries = state.get("max_retries", 2)
    status = state.get("status")

    if status == "retry_patch" and retry_count < max_retries:
        return "generate_patch"

    # Either "patched" (verified) or "max_retries_exceeded"
    return str(END)


def create_review_graph(
    gemini_client: GeminiClientProtocol,
    docker_client: Any = None,
) -> CompiledStateGraph:
    """Construct and compile the LangGraph review-test-patch state machine."""
    workflow = StateGraph(AgentState)

    # Node wrappers injecting dependencies
    async def _analyze_step(state: AgentState) -> dict[str, Any]:
        return await analyze_diff_node(state, gemini_client=gemini_client)

    async def _gen_repro_step(state: AgentState) -> dict[str, Any]:
        return await generate_reproduction_node(state, gemini_client=gemini_client)

    async def _run_repro_step(state: AgentState) -> dict[str, Any]:
        return await run_reproduction_node(state, docker_client=docker_client)

    async def _gen_patch_step(state: AgentState) -> dict[str, Any]:
        return await generate_patch_node(state, gemini_client=gemini_client)

    async def _verify_patch_step(state: AgentState) -> dict[str, Any]:
        return await verify_patch_node(state, docker_client=docker_client)

    # Add nodes
    workflow.add_node("analyze_diff", _analyze_step)
    workflow.add_node("generate_reproduction", _gen_repro_step)
    workflow.add_node("run_reproduction", _run_repro_step)
    workflow.add_node("generate_patch", _gen_patch_step)
    workflow.add_node("verify_patch", _verify_patch_step)

    # Set entry point
    workflow.set_entry_point("analyze_diff")

    # Add conditional edges
    workflow.add_conditional_edges(
        "analyze_diff",
        route_after_analyze,
        {"generate_reproduction": "generate_reproduction", END: END},
    )
    workflow.add_conditional_edges(
        "generate_reproduction",
        route_after_generate_repro,
        {"run_reproduction": "run_reproduction", END: END},
    )
    workflow.add_conditional_edges(
        "run_reproduction",
        route_after_run_repro,
        {"generate_patch": "generate_patch", END: END},
    )
    workflow.add_conditional_edges(
        "generate_patch",
        route_after_generate_patch,
        {"verify_patch": "verify_patch", END: END},
    )
    workflow.add_conditional_edges(
        "verify_patch",
        route_after_verify_patch,
        {"generate_patch": "generate_patch", END: END},
    )

    return workflow.compile()
