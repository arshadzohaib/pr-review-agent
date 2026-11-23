"""Symbol extraction and hunk-to-symbol resolution using Tree-sitter.

Extracts functions, async functions, classes, and methods, and maps diff hunks
to their exact enclosing symbols.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.ast_engine.parser import parse_source
from app.core.diff_parser import FileDiff, Hunk


class SymbolKind(StrEnum):
    FUNCTION = "function"
    ASYNC_FUNCTION = "async_function"
    METHOD = "method"
    CLASS = "class"


class Symbol(BaseModel):
    """Extracted code symbol with line span and signature information."""

    model_config = ConfigDict(frozen=True)

    name: str
    simple_name: str
    kind: SymbolKind
    file_path: str
    start_line: int  # 1-indexed
    end_line: int  # 1-indexed
    signature: str
    docstring: str | None = None
    source_code: str = ""
    class_name: str | None = None
    is_async: bool = False
    attributes: list[str] = Field(default_factory=list)

    @property
    def line_count(self) -> int:
        return max(1, self.end_line - self.start_line + 1)


class HunkSymbolMatch(BaseModel):
    """Result of mapping a diff hunk to its enclosing symbol."""

    model_config = ConfigDict(frozen=True)

    hunk: Hunk
    symbol: Symbol | None = None
    enclosing_class: Symbol | None = None
    fallback_lines: tuple[int, int] | None = None


def _get_node_lines(node: Any) -> tuple[int, int]:
    """Return 1-indexed (start_line, end_line) from Tree-sitter node."""
    start_line = int(node.start_point.row) + 1
    end_row = int(node.end_point.row)
    end_col = int(node.end_point.column)

    end_line = (
        end_row if end_col == 0 and end_row > int(node.start_point.row) else end_row + 1
    )

    return start_line, max(start_line, end_line)


def _extract_docstring(body_node: Any, lines: list[str]) -> str | None:
    """Extract docstring from the first statement of a body block if present."""
    if not body_node:
        return None
    for child in body_node.children:
        if child.type == "expression_statement":
            for expr_child in child.children:
                if expr_child.type == "string":
                    start_l, end_l = _get_node_lines(expr_child)
                    raw = "\n".join(lines[start_l - 1 : end_l])
                    clean = raw.strip()
                    for quote in ('"""', "'''", '"', "'"):
                        if clean.startswith(quote) and clean.endswith(quote):
                            return clean[len(quote) : -len(quote)].strip()
                    return clean
        elif child.type not in ("comment", "\n"):
            break
    return None


def _extract_attributes_from_init(init_node: Any, lines: list[str]) -> list[str]:
    """Find self.<attr> assignments in __init__ body."""
    attributes: list[str] = []
    body = init_node.child_by_field_name("body")
    if not body:
        return attributes

    for stmt in body.children:
        if stmt.type == "expression_statement":
            assignment = stmt.children[0] if stmt.children else None
            if assignment and assignment.type == "assignment":
                left = assignment.child_by_field_name("left")
                if left and left.type == "attribute":
                    obj = left.child_by_field_name("object")
                    attr = left.child_by_field_name("attribute")
                    if obj and attr and obj.text.decode() == "self":
                        start_l, _end_l = _get_node_lines(assignment)
                        snippet = lines[start_l - 1].strip()
                        attributes.append(snippet)
    return attributes


def extract_symbols(tree: Any, source: str, file_path: str) -> list[Symbol]:
    """Traverse Tree-sitter AST to extract top-level functions, classes, and methods."""
    if tree is None or not source:
        return []

    lines = source.splitlines()
    symbols: list[Symbol] = []

    def _get_snippet(start_l: int, end_l: int) -> str:
        return "\n".join(lines[start_l - 1 : end_l])

    for node in tree.root_node.children:
        # Check for decorated definitions
        target_node = node
        decorator_start: int | None = None
        if node.type == "decorated_definition":
            decorator_start = _get_node_lines(node)[0]
            for child in node.children:
                if child.type in ("function_definition", "class_definition"):
                    target_node = child
                    break

        if target_node.type == "function_definition":
            name_node = target_node.child_by_field_name("name")
            params_node = target_node.child_by_field_name("parameters")
            ret_node = target_node.child_by_field_name("return_type")

            simple_name = name_node.text.decode() if name_node else "anonymous"
            params = params_node.text.decode() if params_node else "()"
            ret_str = f" -> {ret_node.text.decode()}" if ret_node else ""

            is_async = any(c.type == "async" for c in target_node.children)
            prefix = "async def " if is_async else "def "
            sig = f"{prefix}{simple_name}{params}{ret_str}"

            start_l, end_l = _get_node_lines(target_node)
            if decorator_start is not None:
                start_l = decorator_start

            docstring = _extract_docstring(target_node.child_by_field_name("body"), lines)
            symbols.append(
                Symbol(
                    name=simple_name,
                    simple_name=simple_name,
                    kind=SymbolKind.ASYNC_FUNCTION if is_async else SymbolKind.FUNCTION,
                    file_path=file_path,
                    start_line=start_l,
                    end_line=end_l,
                    signature=sig,
                    docstring=docstring,
                    source_code=_get_snippet(start_l, end_l),
                    is_async=is_async,
                )
            )

        elif target_node.type == "class_definition":
            name_node = target_node.child_by_field_name("name")
            super_node = target_node.child_by_field_name("superclasses")

            class_name = name_node.text.decode() if name_node else "AnonymousClass"
            supers = super_node.text.decode() if super_node else ""
            class_sig = f"class {class_name}{supers}:"

            c_start_l, c_end_l = _get_node_lines(target_node)
            if decorator_start is not None:
                c_start_l = decorator_start

            class_body = target_node.child_by_field_name("body")
            docstring = _extract_docstring(class_body, lines)

            class_attrs: list[str] = []
            class_methods: list[Symbol] = []

            if class_body:
                for child in class_body.children:
                    m_target = child
                    m_dec_start: int | None = None
                    if child.type == "decorated_definition":
                        m_dec_start = _get_node_lines(child)[0]
                        for sub in child.children:
                            if sub.type == "function_definition":
                                m_target = sub
                                break

                    if m_target.type == "function_definition":
                        m_name_node = m_target.child_by_field_name("name")
                        m_params_node = m_target.child_by_field_name("parameters")
                        m_ret_node = m_target.child_by_field_name("return_type")

                        m_simple = m_name_node.text.decode() if m_name_node else "anonymous"
                        m_params = m_params_node.text.decode() if m_params_node else "()"
                        m_ret = f" -> {m_ret_node.text.decode()}" if m_ret_node else ""

                        m_async = any(c.type == "async" for c in m_target.children)
                        m_prefix = "async def " if m_async else "def "
                        m_sig = f"{m_prefix}{m_simple}{m_params}{m_ret}"

                        m_start_l, m_end_l = _get_node_lines(m_target)
                        if m_dec_start is not None:
                            m_start_l = m_dec_start

                        m_doc = _extract_docstring(m_target.child_by_field_name("body"), lines)

                        if m_simple == "__init__":
                            class_attrs = _extract_attributes_from_init(m_target, lines)

                        method_sym = Symbol(
                            name=f"{class_name}.{m_simple}",
                            simple_name=m_simple,
                            kind=SymbolKind.METHOD,
                            file_path=file_path,
                            start_line=m_start_l,
                            end_line=m_end_l,
                            signature=m_sig,
                            docstring=m_doc,
                            source_code=_get_snippet(m_start_l, m_end_l),
                            class_name=class_name,
                            is_async=m_async,
                        )
                        class_methods.append(method_sym)

            class_sym = Symbol(
                name=class_name,
                simple_name=class_name,
                kind=SymbolKind.CLASS,
                file_path=file_path,
                start_line=c_start_l,
                end_line=c_end_l,
                signature=class_sig,
                docstring=docstring,
                source_code=_get_snippet(c_start_l, c_end_l),
                attributes=class_attrs,
            )
            symbols.append(class_sym)
            symbols.extend(class_methods)

    return symbols


def find_enclosing_symbol(
    symbols: list[Symbol], target_start: int, target_end: int
) -> Symbol | None:
    """Find the innermost symbol that encloses or overlaps with the given line range."""
    matches: list[Symbol] = []
    for s in symbols:
        # Check if line range falls within or overlaps with symbol span
        if s.end_line >= target_start and s.start_line <= target_end:
            matches.append(s)

    if not matches:
        return None

    # Prefer methods/functions over classes, then smallest span (innermost)
    def _rank(s: Symbol) -> tuple[int, int]:
        is_fn = s.kind in (
            SymbolKind.FUNCTION,
            SymbolKind.ASYNC_FUNCTION,
            SymbolKind.METHOD,
        )
        kind_prio = 0 if is_fn else 1
        return (kind_prio, s.line_count)

    matches.sort(key=_rank)
    return matches[0]


def map_hunks_to_symbols(file_diff: FileDiff, source: str) -> list[HunkSymbolMatch]:
    """Map each hunk in a file diff to its enclosing Symbol."""
    tree, _spec = parse_source(source, file_diff.path)
    symbols = extract_symbols(tree, source, file_diff.path) if tree is not None else []

    classes_by_name = {s.name: s for s in symbols if s.kind == SymbolKind.CLASS}
    results: list[HunkSymbolMatch] = []

    for hunk in file_diff.hunks:
        target_start = hunk.target_start
        target_end = hunk.target_end

        symbol = find_enclosing_symbol(symbols, target_start, target_end)
        enclosing_class: Symbol | None = None
        fallback_lines: tuple[int, int] | None = None

        if symbol is not None:
            if symbol.class_name and symbol.class_name in classes_by_name:
                enclosing_class = classes_by_name[symbol.class_name]
        else:
            # Fallback for module-level edits: ±15 line window around hunk
            start = max(1, target_start - 15)
            end = target_end + 15
            fallback_lines = (start, end)

        results.append(
            HunkSymbolMatch(
                hunk=hunk,
                symbol=symbol,
                enclosing_class=enclosing_class,
                fallback_lines=fallback_lines,
            )
        )

    return results
