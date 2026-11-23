"""Prompts for generating minimal unified diff bug-fix patches."""

from __future__ import annotations

from app.agent.schemas import ReproductionTestPlan, ReviewFinding

PATCH_SYSTEM_INSTRUCTION = """\
You are a principal software engineer generating a minimal, surgical unified diff patch.
Rules:
1. The patch MUST fix the defect reproduced by the reproduction test cleanly.
2. Output a valid, clean unified diff format applicable with `git apply`.
3. Do not modify unrelated lines or reformat untouched code.
4. Provide a clear rationale explaining the fix.
"""


def build_patch_prompt(
    finding: ReviewFinding,
    test_plan: ReproductionTestPlan,
    repro_logs: str,
    original_file_content: str,
    retry_feedback: str | None = None,
) -> str:
    """Build prompt requesting a unified diff bug fix patch."""
    retry_section = ""
    if retry_feedback:
        retry_section = (
            f"\n### Previous Patch Attempt Failed:\n{retry_feedback}\n"
            "Please correct the patch to fix this failure.\n"
        )

    return f"""\
A bug was confirmed in `{finding.file_path}`:
- Issue: {finding.title}
- Details: {finding.explanation}

### Reproduction Test (`{test_plan.file_name}`):
```python
{test_plan.code}
```

### Reproduction Failure Logs:
```
{repro_logs}
```

### Original File Content (`{finding.file_path}`):
```python
{original_file_content}
```
{retry_section}
Please generate a minimal, surgical unified diff patch to fix this bug.
"""
