"""Workspace preparation and ephemeral directory management for sandbox execution."""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path


def create_sandbox_workspace(
    files: dict[str, str],
    test_code: str | None = None,
    patch_diff: str | None = None,
) -> str:
    """Create a temporary workspace directory outside OneDrive and write repository files.

    Returns the absolute path to the prepared directory.
    """
    temp_dir = tempfile.mkdtemp(prefix="pr_sandbox_")
    base_path = Path(temp_dir)

    # 1. Write repository files
    for rel_path, content in files.items():
        # Prevent path traversal attacks from untrusted diff file paths
        clean_rel = os.path.normpath(rel_path).lstrip("/\\")
        if ".." in clean_rel:
            continue

        target_file = base_path / clean_rel
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text(content, encoding="utf-8", errors="replace")

    # 2. Write reproduction test file if provided
    if test_code is not None:
        test_file = base_path / "test_repro.py"
        test_file.write_text(test_code, encoding="utf-8")

    # 3. Write patch diff if provided
    if patch_diff is not None:
        patch_file = base_path / "patch.diff"
        patch_file.write_text(patch_diff, encoding="utf-8")

    return temp_dir


def cleanup_sandbox_workspace(workspace_dir: str) -> None:
    """Safely delete the temporary sandbox workspace directory."""
    if workspace_dir and os.path.isdir(workspace_dir):
        shutil.rmtree(workspace_dir, ignore_errors=True)


@contextlib.contextmanager
def sandbox_workspace(
    files: dict[str, str],
    test_code: str | None = None,
    patch_diff: str | None = None,
) -> Iterator[str]:
    """Context manager for ephemeral workspace creation with guaranteed cleanup."""
    ws = create_sandbox_workspace(files, test_code=test_code, patch_diff=patch_diff)
    try:
        yield ws
    finally:
        cleanup_sandbox_workspace(ws)
