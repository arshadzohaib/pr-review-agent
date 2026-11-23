"""Unit tests for caller extraction and repository symbol indexing."""

from __future__ import annotations

from app.ast_engine.callers import extract_call_sites, extract_imports
from app.ast_engine.index import RepoIndex
from app.ast_engine.parser import parse_source
from app.ast_engine.symbols import extract_symbols

FILE_A = """
from app.math_utils import multiply

def calculate(x: int) -> int:
    return multiply(x, 2)
"""

FILE_B = """
import os
from app.calc import calculate

def main():
    val = calculate(10)
    print(val)
"""

FILE_C = """
class Worker:
    def run(self):
        # Unrelated call with same name
        calculate("test")
"""


def test_extract_call_sites_and_imports() -> None:
    tree, _ = parse_source(FILE_B, "app/main.py")
    assert tree is not None
    symbols = extract_symbols(tree, FILE_B, "app/main.py")

    calls = extract_call_sites(tree, FILE_B, "app/main.py", symbols)
    imports = extract_imports(tree, FILE_B, "app/main.py")

    callee_names = [c.callee_name for c in calls]
    assert "calculate" in callee_names
    assert "print" in callee_names

    imported_names = [i.imported_name for i in imports]
    assert "os" in imported_names
    assert "calculate" in imported_names


def test_repo_index_cross_file_callers() -> None:
    files = {
        "app/calc.py": FILE_A,
        "app/main.py": FILE_B,
        "app/worker.py": FILE_C,
    }

    index = RepoIndex.build_from_files(files)

    # Lookup calculate symbol in app/calc.py
    calc_symbols = index.symbols_by_file["app/calc.py"]
    calc_sym = next(s for s in calc_symbols if s.name == "calculate")

    # Find direct callers
    callers = index.find_direct_callers(calc_sym, max_callers=3)
    assert len(callers) >= 2

    # app/main.py imports app.calc, so it must be ranked higher than app/worker.py
    assert callers[0].file_path == "app/main.py"
    assert callers[0].symbol == "main"
    assert "val = calculate(10)" in callers[0].snippet
