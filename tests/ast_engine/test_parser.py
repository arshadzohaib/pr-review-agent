"""Unit tests for Tree-sitter language registry and safe parser."""

from __future__ import annotations

from app.ast_engine.languages import (
    PYTHON_LANGUAGE_SPEC,
    LanguageRegistry,
)
from app.ast_engine.parser import parse_source


def test_language_registry_lookups() -> None:
    reg = LanguageRegistry()
    assert reg.get_for_extension(".py") == PYTHON_LANGUAGE_SPEC
    assert reg.get_for_extension(".PY") == PYTHON_LANGUAGE_SPEC
    assert reg.get_for_path("app/core/calc.py") == PYTHON_LANGUAGE_SPEC
    assert reg.get_for_path("main.unknown") is None


def test_parse_source_valid_python() -> None:
    code = "def add(a: int, b: int) -> int:\n    return a + b\n"
    tree, spec = parse_source(code, "calc.py")

    assert tree is not None
    assert spec is not None
    assert spec.name == "python"
    assert tree.root_node.type == "module"


def test_parse_source_broken_syntax_graceful_fallback() -> None:
    # Partially typed / broken Python code from incomplete PR edits
    broken_code = "def broken_func(\n    x = 1 +\n"
    tree, spec = parse_source(broken_code, "broken.py")

    # Tree-sitter must produce a syntax tree with error nodes without crashing
    assert tree is not None
    assert spec is not None
    assert tree.root_node.has_error is True


def test_parse_source_unsupported_language() -> None:
    code = "fn main() { println!(\"hello\"); }"
    tree, spec = parse_source(code, "main.rs")

    assert tree is None
    assert spec is None
