"""Safe Tree-sitter parser factory and execution.

Tree-sitter produces error-tolerant concrete syntax trees (CST), allowing analysis
even on syntactically broken or partially edited pull-request files.
"""

from __future__ import annotations

import logging
from typing import Any

import tree_sitter

from app.ast_engine.languages import DEFAULT_REGISTRY, LanguageRegistry, LanguageSpec

logger = logging.getLogger(__name__)


def get_parser_for_spec(spec: LanguageSpec) -> tree_sitter.Parser:
    """Create a new Tree-sitter Parser configured with the language grammar."""
    lang = spec.get_language()
    return tree_sitter.Parser(lang)


def parse_bytes(source_bytes: bytes, spec: LanguageSpec) -> Any:
    """Parse raw bytes into a Tree-sitter Tree."""
    parser = get_parser_for_spec(spec)
    return parser.parse(source_bytes)


def parse_source(
    source: str,
    path: str,
    registry: LanguageRegistry = DEFAULT_REGISTRY,
) -> tuple[Any | None, LanguageSpec | None]:
    """Parse string source code for a file path.

    Returns (tree, spec) or (None, spec) if unsupported language or encoding failure.
    Does not crash on syntax errors.
    """
    spec = registry.get_for_path(path)
    if spec is None:
        return None, None

    try:
        source_bytes = source.encode("utf-8")
        tree = parse_bytes(source_bytes, spec)
        return tree, spec
    except Exception as exc:
        logger.warning("Error parsing source for %s: %s", path, exc)
        return None, spec
