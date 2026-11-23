"""Pluggable Tree-sitter language grammar registry.

Enables multi-language support while keeping Python as the primary v1 implementation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

import tree_sitter
import tree_sitter_python


@dataclass(frozen=True)
class LanguageSpec:
    """Specification and node type registry for a supported programming language."""

    name: str
    extensions: tuple[str, ...]
    grammar_loader: Callable[[], Any]
    function_node_types: frozenset[str]
    class_node_types: frozenset[str]
    call_node_types: frozenset[str]
    import_node_types: frozenset[str]

    def get_language(self) -> Any:
        """Instantiate and return the tree_sitter.Language."""
        return tree_sitter.Language(self.grammar_loader())


def _load_python_grammar() -> Any:
    return tree_sitter_python.language()


PYTHON_LANGUAGE_SPEC: Final[LanguageSpec] = LanguageSpec(
    name="python",
    extensions=(".py", ".pyi"),
    grammar_loader=_load_python_grammar,
    function_node_types=frozenset({"function_definition"}),
    class_node_types=frozenset({"class_definition"}),
    call_node_types=frozenset({"call"}),
    import_node_types=frozenset({"import_statement", "import_from_statement"}),
)


class LanguageRegistry:
    """Registry mapping file extensions to tree-sitter language specs."""

    def __init__(self) -> None:
        self._by_extension: dict[str, LanguageSpec] = {}
        self._by_name: dict[str, LanguageSpec] = {}
        # Register default languages
        self.register(PYTHON_LANGUAGE_SPEC)

    def register(self, spec: LanguageSpec) -> None:
        """Register a new language grammar specification."""
        self._by_name[spec.name.lower()] = spec
        for ext in spec.extensions:
            self._by_extension[ext.lower()] = spec

    def get_for_extension(self, extension: str) -> LanguageSpec | None:
        """Lookup language spec by file extension (e.g. '.py')."""
        return self._by_extension.get(extension.lower())

    def get_for_path(self, path: str) -> LanguageSpec | None:
        """Lookup language spec by file path."""
        name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if "." in name:
            ext = "." + name.rsplit(".", 1)[-1].lower()
            return self.get_for_extension(ext)
        return None

    def get_by_name(self, name: str) -> LanguageSpec | None:
        """Lookup language spec by name (e.g. 'python')."""
        return self._by_name.get(name.lower())


DEFAULT_REGISTRY: Final[LanguageRegistry] = LanguageRegistry()
