"""Prompts for PR diff and AST context analysis."""

from __future__ import annotations

from typing import Any

REVIEW_SYSTEM_INSTRUCTION = """\
You are an expert senior software engineer and security auditor reviewing a GitHub pull request.
Your job is to identify REAL, high-confidence bugs, security vulnerabilities, or broken logic.
Ignore purely cosmetic or style issues (formatting, naming, docstrings) unless they cause real bugs.
For each issue, specify:
- file_path
- line (approximate line number in head file)
- severity: "info", "minor", "major", or "critical"
- category: "bug", "security", "performance", "style", or "maintainability"
- confidence: float between 0.0 and 1.0 (only report if confidence >= 0.7)
- title: concise summary
- explanation: clear explanation of the defect and impact
- reproducible: true if this bug can be confirmed with a deterministic pytest unit test.

If the code is clean or changes are safe, return an empty list of findings.
"""


def build_review_prompt(diff_hunks: list[Any], ast_context: list[Any]) -> str:
    """Build compact, structured review prompt containing unified diffs and AST context."""
    chunks_text: list[str] = []

    # 1. Format AST contexts if available
    for ctx in ast_context:
        func_src = getattr(ctx, "function_source", str(ctx))
        cls_header = getattr(ctx, "class_header", None)
        callers = getattr(ctx, "callers", [])

        chunk_str = f"### Symbol Context:\n```python\n{func_src}\n```"
        if cls_header:
            chunk_str = f"Class: {cls_header}\n" + chunk_str
        if callers:
            caller_snippets = "\n".join(
                f"- Caller `{c.symbol}` in `{c.file_path}:{c.line}`:\n```python\n{c.snippet}\n```"
                for c in callers
            )
            chunk_str += f"\nDirect 1-Hop Callers:\n{caller_snippets}"
        chunks_text.append(chunk_str)

    # 2. Format diff hunks
    hunks_text: list[str] = []
    for h in diff_hunks:
        if hasattr(h, "hunk"):
            file_path = getattr(h, "file_path", "unknown")
            hunks_text.append(f"File: {file_path}\n```diff\n{h.hunk}\n```")
        elif isinstance(h, str):
            hunks_text.append(f"```diff\n{h}\n```")
        elif isinstance(h, dict):
            fp = h.get("file_path", "unknown")
            hunk_content = h.get("hunk", str(h))
            hunks_text.append(f"File: {fp}\n```diff\n{hunk_content}\n```")

    diff_section = "\n\n".join(hunks_text) if hunks_text else "No diff hunks provided."
    ast_section = "\n\n".join(chunks_text) if chunks_text else "No extra AST context."

    return f"""\
Please review the following PR diff hunks and AST symbol contexts:

## AST Symbol & 1-Hop Caller Context:
{ast_section}

## PR Unified Diff Hunks:
{diff_section}

Analyze the changes thoroughly for real bugs or vulnerabilities.
"""
