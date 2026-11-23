"""Repository-wide AST symbol, caller, and import index.

Provides fast 1-hop cross-file caller discovery for modified functions.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.ast_engine.callers import CallSite, ImportedSymbol, extract_call_sites, extract_imports
from app.ast_engine.parser import parse_source
from app.ast_engine.symbols import Symbol, extract_symbols


class CallerSnippet(BaseModel):
    """Context snippet of a caller invoking a modified symbol."""

    model_config = ConfigDict(frozen=True)

    file_path: str
    symbol: str
    line: int
    snippet: str


class RepoIndex(BaseModel):
    """In-memory symbol, call-site, and import graph of a repository snapshot."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    symbols_by_file: dict[str, list[Symbol]] = Field(default_factory=dict)
    calls_by_file: dict[str, list[CallSite]] = Field(default_factory=dict)
    imports_by_file: dict[str, list[ImportedSymbol]] = Field(default_factory=dict)
    symbols_by_name: dict[str, list[Symbol]] = Field(default_factory=dict)

    @classmethod
    def build_from_files(cls, files: dict[str, str]) -> RepoIndex:
        """Parse and index a mapping of {file_path: file_content}."""
        index = cls()

        for path, content in files.items():
            tree, spec = parse_source(content, path)
            if tree is None or spec is None:
                continue

            symbols = extract_symbols(tree, content, path)
            calls = extract_call_sites(tree, content, path, symbols)
            imports = extract_imports(tree, content, path)

            index.symbols_by_file[path] = symbols
            index.calls_by_file[path] = calls
            index.imports_by_file[path] = imports

            for sym in symbols:
                index.symbols_by_name.setdefault(sym.simple_name, []).append(sym)
                if sym.class_name:
                    index.symbols_by_name.setdefault(sym.name, []).append(sym)

        return index

    def _file_imports_target(self, file_path: str, target: Symbol) -> bool:
        """Check if file_path imports target symbol's module or name directly."""
        imports = self.imports_by_file.get(file_path, [])
        target_mod = target.file_path.replace("/", ".").replace("\\", ".").removesuffix(".py")

        for imp in imports:
            if imp.imported_name in (target.simple_name, "*"):
                return True
            if imp.module and (imp.module in target_mod or target_mod.endswith(imp.module)):
                return True
        return False

    def find_direct_callers(
        self, target_symbol: Symbol, max_callers: int = 3
    ) -> list[CallerSnippet]:
        """Find and rank up to max_callers direct callers of target_symbol."""
        target_name = target_symbol.simple_name
        target_path = target_symbol.file_path

        candidates: list[CallSite] = []
        for file_path, calls in self.calls_by_file.items():
            for call in calls:
                if call.callee_base == target_name:
                    # Avoid self-referencing recursive calls in the same function
                    if (
                        file_path == target_path
                        and call.enclosing_symbol_name == target_symbol.name
                    ):
                        continue
                    candidates.append(call)

        def _rank(call: CallSite) -> tuple[int, int]:
            # Priority 0: same file
            if call.file_path == target_path:
                return (0, call.line)
            # Priority 1: files that import the defining module/symbol
            if self._file_imports_target(call.file_path, target_symbol):
                return (1, call.line)
            # Priority 2: other name matches
            return (2, call.line)

        candidates.sort(key=_rank)

        snippets: list[CallerSnippet] = []
        seen_sites: set[tuple[str, int]] = set()

        for c in candidates:
            if len(snippets) >= max_callers:
                break
            site_key = (c.file_path, c.line)
            if site_key in seen_sites:
                continue
            seen_sites.add(site_key)

            caller_name = c.enclosing_symbol_name or c.callee_name
            snippets.append(
                CallerSnippet(
                    file_path=c.file_path,
                    symbol=caller_name,
                    line=c.line,
                    snippet=c.snippet,
                )
            )

        return snippets
