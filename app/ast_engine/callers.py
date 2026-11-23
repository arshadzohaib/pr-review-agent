"""Extraction of call-sites and imports across files using Tree-sitter.

Pinpoints 1-hop callers and module dependencies without requiring a full type solver.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from app.ast_engine.symbols import Symbol


class CallSite(BaseModel):
    """An invocation/call site in the codebase."""

    model_config = ConfigDict(frozen=True)

    callee_name: str  # e.g. "add", "c.add", "Calculator"
    callee_base: str  # e.g. "add" for "c.add"
    file_path: str
    line: int  # 1-indexed
    enclosing_symbol_name: str | None = None
    snippet: str = ""


class ImportedSymbol(BaseModel):
    """An imported module or symbol reference."""

    model_config = ConfigDict(frozen=True)

    module: str  # e.g. "app.calc", "math"
    imported_name: str  # e.g. "add", "sqrt", "*"
    alias: str | None = None
    file_path: str
    line: int = 1


def _walk_tree(node: Any) -> list[Any]:
    """Depth-first traversal collecting all AST nodes."""
    nodes: list[Any] = []
    stack = [node]
    while stack:
        current = stack.pop()
        nodes.append(current)
        for child in reversed(current.children):
            stack.append(child)
    return nodes


def _make_call_snippet(lines: list[str], call_line: int, enclosing_sig: str | None) -> str:
    """Format a concise 2-4 line snippet containing caller context."""
    start_idx = max(0, call_line - 2)
    end_idx = min(len(lines), call_line + 1)
    surrounding = "\n".join(lines[start_idx:end_idx]).rstrip()

    if enclosing_sig:
        return f"{enclosing_sig}\n    ...\n{surrounding}"
    return surrounding


def extract_call_sites(
    tree: Any, source: str, file_path: str, symbols: list[Symbol] | None = None
) -> list[CallSite]:
    """Find all function/method/constructor call sites in an AST."""
    if tree is None or not source:
        return []

    lines = source.splitlines()
    call_sites: list[CallSite] = []

    for node in _walk_tree(tree.root_node):
        if node.type == "call":
            fn_node = node.child_by_field_name("function")
            if not fn_node:
                continue

            callee_name = fn_node.text.decode("utf-8", errors="replace")
            # Extract callee base (simple name)
            if fn_node.type == "attribute":
                attr_node = fn_node.child_by_field_name("attribute")
                if attr_node:
                    callee_base = attr_node.text.decode("utf-8", errors="replace")
                else:
                    callee_base = callee_name
            else:
                callee_base = callee_name

            line = int(node.start_point.row) + 1

            # Match enclosing symbol
            enclosing_name: str | None = None
            enclosing_sig: str | None = None
            if symbols:
                for sym in symbols:
                    if sym.start_line <= line <= sym.end_line:
                        enclosing_name = sym.name
                        enclosing_sig = sym.signature
                        break

            snippet = _make_call_snippet(lines, line, enclosing_sig)

            call_sites.append(
                CallSite(
                    callee_name=callee_name,
                    callee_base=callee_base,
                    file_path=file_path,
                    line=line,
                    enclosing_symbol_name=enclosing_name,
                    snippet=snippet,
                )
            )

    return call_sites


def extract_imports(tree: Any, source: str, file_path: str) -> list[ImportedSymbol]:
    """Extract all import statements and their aliases."""
    if tree is None or not source:
        return []

    imports: list[ImportedSymbol] = []

    for node in _walk_tree(tree.root_node):
        line = int(node.start_point.row) + 1

        if node.type == "import_statement":
            # import foo, bar as b
            for child in node.children:
                if child.type == "dotted_name":
                    name = child.text.decode("utf-8", errors="replace")
                    imports.append(
                        ImportedSymbol(
                            module=name,
                            imported_name=name,
                            file_path=file_path,
                            line=line,
                        )
                    )
                elif child.type == "aliased_import":
                    name_node = child.child_by_field_name("name")
                    alias_node = child.child_by_field_name("alias")
                    name = name_node.text.decode("utf-8", errors="replace") if name_node else ""
                    alias = (
                        alias_node.text.decode("utf-8", errors="replace")
                        if alias_node
                        else None
                    )
                    if name:
                        imports.append(
                            ImportedSymbol(
                                module=name,
                                imported_name=name,
                                alias=alias,
                                file_path=file_path,
                                line=line,
                            )
                        )

        elif node.type == "import_from_statement":
            # from foo import bar, baz as b
            mod_node = node.child_by_field_name("module_name")
            mod_name = mod_node.text.decode("utf-8", errors="replace") if mod_node else ""

            # Check after the 'import' keyword
            seen_import = False
            for child in node.children:
                if child.type == "import":
                    seen_import = True
                    continue
                if not seen_import:
                    continue

                if child.type == "dotted_name":
                    item_name = child.text.decode("utf-8", errors="replace")
                    imports.append(
                        ImportedSymbol(
                            module=mod_name,
                            imported_name=item_name,
                            file_path=file_path,
                            line=line,
                        )
                    )
                elif child.type == "aliased_import":
                    name_node = child.child_by_field_name("name")
                    alias_node = child.child_by_field_name("alias")
                    item_name = (
                        name_node.text.decode("utf-8", errors="replace") if name_node else ""
                    )
                    alias = (
                        alias_node.text.decode("utf-8", errors="replace")
                        if alias_node
                        else None
                    )
                    if item_name:
                        imports.append(
                            ImportedSymbol(
                                module=mod_name,
                                imported_name=item_name,
                                alias=alias,
                                file_path=file_path,
                                line=line,
                            )
                        )
                elif child.type == "wildcard_import":
                    imports.append(
                        ImportedSymbol(
                            module=mod_name,
                            imported_name="*",
                            file_path=file_path,
                            line=line,
                        )
                    )

    return imports
