"""Unit tests for app.core.diff_parser."""

from __future__ import annotations

import pytest

from app.core.diff_parser import FileStatus, parse_diff

SAMPLE_DIFF = """diff --git a/app/calc.py b/app/calc.py
index 1111111..2222222 100644
--- a/app/calc.py
+++ b/app/calc.py
@@ -1,3 +1,4 @@
 def add(a, b):
-    return a - b
+    # fixed bug
+    return a + b
 def sub(a, b):
diff --git a/app/old.py b/app/new.py
similarity index 100%
rename from app/old.py
rename to app/new.py
diff --git a/deleted.py b/deleted.py
deleted file mode 100644
index 3333333..0000000
--- a/deleted.py
+++ /dev/null
@@ -1,2 +0,0 @@
-x = 1
-y = 2
diff --git a/assets/logo.png b/assets/logo.png
index 4444444..5555555 100644
Binary files a/assets/logo.png and b/assets/logo.png differ
"""


def test_parse_diff_empty() -> None:
    assert parse_diff("") == []
    assert parse_diff("   \n  ") == []


def test_parse_diff_files() -> None:
    files = parse_diff(SAMPLE_DIFF)
    assert len(files) == 4

    calc = files[0]
    assert calc.path == "app/calc.py"
    assert calc.status == FileStatus.MODIFIED
    assert not calc.is_binary
    assert len(calc.hunks) == 1

    hunk = calc.hunks[0]
    assert hunk.source_start == 1
    assert hunk.target_start == 1
    assert hunk.added == ["    # fixed bug", "    return a + b"]
    assert hunk.removed == ["    return a - b"]
    assert calc.changed_line_count == 3
    assert calc.extension == ".py"

    renamed = files[1]
    assert renamed.path == "app/new.py"
    assert renamed.old_path == "app/old.py"
    assert renamed.status == FileStatus.RENAMED
    assert len(renamed.hunks) == 0

    deleted = files[2]
    assert deleted.path == "deleted.py"
    assert deleted.status == FileStatus.DELETED
    assert deleted.changed_line_count == 2
    assert len(deleted.added_lines) == 0

    binary = files[3]
    assert binary.path == "assets/logo.png"
    assert binary.is_binary
    assert binary.changed_line_count == 0


def test_parse_diff_malformed() -> None:
    bad_diff = """diff --git a/a.txt b/a.txt
--- a/a.txt
+++ b/a.txt
@@ -1,1 +1,1 @@
invalid_line_without_prefix
"""
    with pytest.raises(ValueError, match="malformed unified diff"):
        parse_diff(bad_diff)
