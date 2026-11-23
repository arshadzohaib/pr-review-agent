"""Unit tests for AST context building and token budgeting."""

from __future__ import annotations

from app.ast_engine.context_builder import (
    ASTContext,
    build_ast_context_for_match,
    pack_ast_contexts,
)
from app.ast_engine.index import RepoIndex
from app.ast_engine.symbols import map_hunks_to_symbols
from app.core.diff_parser import parse_diff

FILE_CONTENT = """class Service:
    def __init__(self):
        self.active = True

    def run_task(self, item_id: int) -> bool:
        return self.active
"""

DIFF_TEXT = """diff --git a/service.py b/service.py
--- a/service.py
+++ b/service.py
@@ -5,2 +5,2 @@
-    def run_task(self, item_id: int) -> bool:
-        return self.active
+    def run_task(self, item_id: int) -> bool:
+        return self.active and item_id > 0
"""


def test_build_ast_context_and_budget() -> None:
    file_diff = parse_diff(DIFF_TEXT)[0]
    matches = map_hunks_to_symbols(file_diff, FILE_CONTENT)
    assert len(matches) == 1

    index = RepoIndex.build_from_files({"service.py": FILE_CONTENT})

    # Normal budget
    chunk, ctx = build_ast_context_for_match(
        matches[0],
        file_path="service.py",
        index=index,
        max_tokens=6000,
    )

    assert chunk.symbol == "Service.run_task"
    assert len(chunk.chunk_sha) == 64
    assert ctx.class_header is not None
    assert "class Service:" in ctx.class_header
    assert "self.active = True" in ctx.class_header
    assert ctx.est_tokens > 0

    # Strict token budget: should drop class_header
    _, tight_ctx = build_ast_context_for_match(
        matches[0],
        file_path="service.py",
        index=index,
        max_tokens=20,  # very tight budget
    )
    # The function itself is preserved, but optional context is dropped
    assert tight_ctx.function_source != ""
    assert tight_ctx.class_header is None
    assert len(tight_ctx.callers) == 0


def test_pack_ast_contexts_batching() -> None:
    c1 = ASTContext(
        chunk_sha="sha1",
        file_path="a.py",
        symbol_name="f1",
        function_source="def f1(): pass",
        diff_hunk="@@ hunk 1 @@",
        est_tokens=300,
    )
    c2 = ASTContext(
        chunk_sha="sha2",
        file_path="b.py",
        symbol_name="f2",
        function_source="def f2(): pass",
        diff_hunk="@@ hunk 2 @@",
        est_tokens=400,
    )
    c3 = ASTContext(
        chunk_sha="sha3",
        file_path="c.py",
        symbol_name="f3",
        function_source="def f3(): pass",
        diff_hunk="@@ hunk 3 @@",
        est_tokens=500,
    )

    # Fits together in 1000 tokens (c1: 300 + c2: 400 = 700 <= 1000, c3 goes to next batch)
    batches = pack_ast_contexts([c1, c2, c3], max_tokens_per_batch=800)
    assert len(batches) == 2
    assert len(batches[0]) == 2
    assert len(batches[1]) == 1
