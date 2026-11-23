"""Unit tests for sandbox workspace creation and path traversal guards."""

from __future__ import annotations

import os
from pathlib import Path

from app.sandbox.workspace import (
    cleanup_sandbox_workspace,
    create_sandbox_workspace,
    sandbox_workspace,
)


def test_create_and_cleanup_workspace() -> None:
    files = {
        "app/calc.py": "def add(a, b): return a + b",
        "app/utils/math.py": "PI = 3.14",
    }
    test_code = "def test_add(): assert add(1, 2) == 3"
    patch_diff = "diff --git a/app/calc.py b/app/calc.py\n"

    ws_dir = create_sandbox_workspace(files, test_code=test_code, patch_diff=patch_diff)

    try:
        assert os.path.isdir(ws_dir)
        # Verify files were written
        calc_file = Path(ws_dir) / "app" / "calc.py"
        assert calc_file.is_file()
        assert "def add" in calc_file.read_text(encoding="utf-8")

        test_file = Path(ws_dir) / "test_repro.py"
        assert test_file.is_file()
        assert "def test_add" in test_file.read_text(encoding="utf-8")

        patch_file = Path(ws_dir) / "patch.diff"
        assert patch_file.is_file()
    finally:
        cleanup_sandbox_workspace(ws_dir)

    # Verify cleaned up
    assert not os.path.exists(ws_dir)


def test_workspace_traversal_guard() -> None:
    files = {
        "../../etc/passwd": "malicious content",
        "valid/file.py": "print('ok')",
    }
    with sandbox_workspace(files) as ws_dir:
        # Path with .. should be discarded
        assert not (Path(ws_dir).parent / "etc" / "passwd").exists()
        assert (Path(ws_dir) / "valid" / "file.py").is_file()
