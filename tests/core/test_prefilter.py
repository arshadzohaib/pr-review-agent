"""Unit tests for app.core.prefilter."""

from __future__ import annotations

import pytest

from app.core.diff_parser import parse_diff
from app.core.prefilter import (
    PullRequestMeta,
    SkipReason,
    SourcePair,
    check_event,
    classify_file,
    classify_path,
    is_formatting_only,
    is_import_reorder_only,
    python_ast_fingerprint,
    run_prefilter,
)


def test_check_event() -> None:
    # Allowed
    meta = PullRequestMeta(action="opened", author_login="alice")
    assert check_event(meta) is None

    # Unsupported action
    meta = PullRequestMeta(action="closed")
    assert check_event(meta) == SkipReason.UNSUPPORTED_ACTION

    # Draft
    meta = PullRequestMeta(action="opened", draft=True)
    assert check_event(meta) == SkipReason.DRAFT

    # Bot author
    meta = PullRequestMeta(action="opened", author_login="dependabot[bot]")
    assert check_event(meta) == SkipReason.BOT_AUTHOR

    meta = PullRequestMeta(action="opened", author_type="Bot")
    assert check_event(meta) == SkipReason.BOT_AUTHOR

    # Skip label
    meta = PullRequestMeta(action="opened", labels=frozenset({"skip-ai-review"}))
    assert check_event(meta) == SkipReason.SKIP_LABEL


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("package-lock.json", SkipReason.LOCKFILE),
        ("poetry.lock", SkipReason.LOCKFILE),
        ("README.md", SkipReason.DOCS),
        ("docs/architecture.rst", SkipReason.DOCS),
        ("LICENSE", SkipReason.DOCS),
        (".github/issue_template/bug.yml", SkipReason.DOCS),
        ("image.png", SkipReason.ASSET),
        ("bundle.zip", SkipReason.ASSET),
        ("vendor/bundle.js", SkipReason.GENERATED),
        ("proto/service_pb2.py", SkipReason.GENERATED),
        ("app/main.py", None),
    ],
)
def test_classify_path(path: str, expected: SkipReason | None) -> None:
    assert classify_path(path) == expected


def test_python_ast_fingerprint_comments_and_docstrings() -> None:
    src1 = """
def foo(x: int) -> int:
    \"\"\"This is a docstring.\"\"\"
    # Comment A
    return x + 1
"""
    src2 = """
def foo(x: int) -> int:
    # Different comment
    return x + 1
"""
    src3 = """
def foo(x: int) -> int:
    return x + 2
"""
    fp1 = python_ast_fingerprint(src1)
    fp2 = python_ast_fingerprint(src2)
    fp3 = python_ast_fingerprint(src3)

    assert fp1 is not None and fp2 is not None and fp3 is not None
    assert fp1 == fp2  # Docstring and comment differences ignored
    assert fp1 != fp3  # Semantic code change detected


def test_is_formatting_only_ast_and_hunk() -> None:
    diff_text = """diff --git a/app/calc.py b/app/calc.py
--- a/app/calc.py
+++ b/app/calc.py
@@ -1,1 +1,3 @@
-def foo(): return 1
+def foo():
+    # comment
+    return 1
"""
    files = parse_diff(diff_text)
    fd = files[0]

    base = "def foo(): return 1\n"
    head = "def foo():\n    # comment\n    return 1\n"

    assert is_formatting_only(fd, SourcePair(base, head)) is True


def test_is_import_reorder_only() -> None:
    diff_text = """diff --git a/app/calc.py b/app/calc.py
--- a/app/calc.py
+++ b/app/calc.py
@@ -1,2 +1,2 @@
-import sys
 import os
+import sys
"""
    files = parse_diff(diff_text)
    assert is_import_reorder_only(files[0]) is True


def test_classify_file_unsupported_language_and_deletions() -> None:
    diff_ts = """diff --git a/app/calc.ts b/app/calc.ts
--- a/app/calc.ts
+++ b/app/calc.ts
@@ -1,1 +1,1 @@
-const x = 1;
+const x = 2;
"""
    fd_ts = parse_diff(diff_ts)[0]
    assert classify_file(fd_ts) == SkipReason.UNSUPPORTED_LANGUAGE

    diff_del = """diff --git a/app/calc.py b/app/calc.py
--- a/app/calc.py
+++ b/app/calc.py
@@ -1,2 +0,0 @@
-def old():
-    pass
"""
    fd_del = parse_diff(diff_del)[0]
    assert classify_file(fd_del) == SkipReason.DELETED_ONLY


def test_run_prefilter_full_pipeline() -> None:
    diff_text = """diff --git a/app/main.py b/app/main.py
--- a/app/main.py
+++ b/app/main.py
@@ -1,1 +1,1 @@
-def run(): return 1
+def run(): return 2
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,1 +1,1 @@
-# Old
+# New
"""
    files = parse_diff(diff_text)
    meta = PullRequestMeta(action="opened", author_login="dev")
    res = run_prefilter(meta, files)

    assert res.has_work is True
    assert len(res.kept) == 1
    assert res.kept[0].path == "app/main.py"
    assert res.skipped["README.md"] == SkipReason.DOCS
