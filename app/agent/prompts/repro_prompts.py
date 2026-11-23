"""Prompts for generating focused pytest reproduction tests."""

from __future__ import annotations

from app.agent.schemas import ReviewFinding

REPRO_SYSTEM_INSTRUCTION = """\
You are an expert test engineer writing an isolated Python pytest unit test.
Rules:
1. The test must be completely standalone in a single file `test_repro.py`.
2. Import the modified module/functions using standard Python imports from the workspace.
3. Do NOT import third-party libraries not already in standard Python library or pytest.
4. The test must FAIL on the buggy implementation, and PASS once properly fixed.
5. Keep the test minimal, focused, and deterministic. No network calls or sleeps.
"""


def build_repro_prompt(finding: ReviewFinding, file_content: str | None = None) -> str:
    """Build prompt requesting a pytest reproduction test for the given finding."""
    source_snippet = (
        f"Target File Content (`{finding.file_path}`):\n```python\n{file_content}\n```"
        if file_content
        else f"Target File: `{finding.file_path}` at line {finding.line}"
    )

    return f"""\
We identified a suspected bug in `{finding.file_path}`:
- Title: {finding.title}
- Line: {finding.line}
- Severity: {finding.severity}
- Category: {finding.category}
- Explanation: {finding.explanation}

{source_snippet}

Please write a self-contained Python `pytest` test in `test_repro.py`
that confirms and reproduces this bug.
"""
