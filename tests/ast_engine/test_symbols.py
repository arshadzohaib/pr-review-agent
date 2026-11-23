"""Unit tests for symbol extraction and hunk-to-symbol resolution."""

from __future__ import annotations

from app.ast_engine.parser import parse_source
from app.ast_engine.symbols import (
    SymbolKind,
    extract_symbols,
    find_enclosing_symbol,
    map_hunks_to_symbols,
)
from app.core.diff_parser import parse_diff

SAMPLE_CODE = """import os

@decorator
def top_level(x: int) -> int:
    \"\"\"Top doc.\"\"\"
    return x * 2

class Calculator:
    \"\"\"Calculator class.\"\"\"
    def __init__(self, base: int):
        self.base = base
        self.count = 0

    async def compute(self, val: int) -> int:
        \"\"\"Async compute.\"\"\"
        return self.base + val

# Module level statement
print("done")
"""


def test_extract_symbols_functions_classes_methods() -> None:
    tree, _ = parse_source(SAMPLE_CODE, "sample.py")
    assert tree is not None

    symbols = extract_symbols(tree, SAMPLE_CODE, "sample.py")
    names = [s.name for s in symbols]

    assert "top_level" in names
    assert "Calculator" in names
    assert "Calculator.__init__" in names
    assert "Calculator.compute" in names

    top_fn = next(s for s in symbols if s.name == "top_level")
    assert top_fn.kind == SymbolKind.FUNCTION
    assert top_fn.docstring == "Top doc."
    assert "@decorator" in top_fn.source_code

    calc_cls = next(s for s in symbols if s.name == "Calculator")
    assert calc_cls.kind == SymbolKind.CLASS
    assert "self.base = base" in calc_cls.attributes

    compute_fn = next(s for s in symbols if s.name == "Calculator.compute")
    assert compute_fn.kind == SymbolKind.METHOD
    assert compute_fn.is_async is True
    assert compute_fn.class_name == "Calculator"


def test_find_enclosing_symbol() -> None:
    tree, _ = parse_source(SAMPLE_CODE, "sample.py")
    symbols = extract_symbols(tree, SAMPLE_CODE, "sample.py")

    # Inside top_level (lines ~3 to 6)
    match1 = find_enclosing_symbol(symbols, 4, 5)
    assert match1 is not None
    assert match1.name == "top_level"

    # Inside Calculator.compute (lines ~14 to 17)
    match2 = find_enclosing_symbol(symbols, 15, 16)
    assert match2 is not None
    assert match2.name == "Calculator.compute"

    # Outside in module level statement (line 20)
    match_none = find_enclosing_symbol(symbols, 20, 20)
    assert match_none is None


def test_map_hunks_to_symbols() -> None:
    diff_text = """diff --git a/sample.py b/sample.py
--- a/sample.py
+++ b/sample.py
@@ -5,1 +5,2 @@
-    return x * 2
+    # modified top_level
+    return x * 4
@@ -19,1 +20,1 @@
-print("done")
+print("finished")
"""
    file_diff = parse_diff(diff_text)[0]
    matches = map_hunks_to_symbols(file_diff, SAMPLE_CODE)

    assert len(matches) == 2

    # Hunk 1 should map to top_level
    assert matches[0].symbol is not None
    assert matches[0].symbol.name == "top_level"

    # Hunk 2 should be module-level fallback
    assert matches[1].symbol is None
    assert matches[1].fallback_lines is not None
