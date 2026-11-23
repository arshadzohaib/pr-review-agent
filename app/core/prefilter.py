"""Zero-LLM static pre-filter.

Pure functions decide, with no network and no model call, which parts of a pull request are
worth sending to Gemini. Rules run cheapest-first (see ARCHITECTURE.md section 3.1):

    event gate -> path rules -> binary -> language -> empty/deleted -> generated header
    -> pure deletions -> formatting-only -> trivial (import reorder) -> size guard

Formatting-only detection, strongest signal first:
  1. Python base+head sources available: compare normalised ASTs (ignores whitespace,
     comments, line wrapping, re-indentation and docstring edits). The AST verdict is final.
  2. Otherwise a conservative hunk-level check: blank and comment-only lines are dropped,
     intra-line whitespace is normalised, but *leading indentation is preserved* (it is
     semantic in Python) and line boundaries are preserved. Re-wrapped code is therefore
     treated as a real change here rather than risking a missed bug.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Final, NamedTuple

from pydantic import BaseModel, ConfigDict, Field

from app.core.diff_parser import FileDiff, FileStatus, Hunk


class SkipReason(StrEnum):
    # PR-level
    UNSUPPORTED_ACTION = "unsupported_action"
    DRAFT = "draft"
    BOT_AUTHOR = "bot_author"
    SKIP_LABEL = "skip_label"
    # file-level
    LOCKFILE = "lockfile"
    DOCS = "docs"
    ASSET = "asset"
    BINARY = "binary"
    GENERATED = "generated"
    UNSUPPORTED_LANGUAGE = "unsupported_language"
    NO_CHANGES = "no_changes"
    DELETED_ONLY = "deleted_only"
    FORMATTING_ONLY = "formatting_only"
    TRIVIAL = "trivial"
    TOO_LARGE = "too_large"


# ---- Configuration constants --------------------------------------------------------------
ALLOWED_ACTIONS: Final[frozenset[str]] = frozenset(
    {"opened", "synchronize", "reopened", "ready_for_review"}
)
SKIP_LABEL: Final[str] = "skip-ai-review"

LOCKFILE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
        "bun.lockb", "bun.lock", "poetry.lock", "pipfile.lock", "uv.lock", "pdm.lock",
        "cargo.lock", "go.sum", "composer.lock", "gemfile.lock", "mix.lock",
        "pubspec.lock", "packages.lock.json", "flake.lock", "conda-lock.yml",
    }
)  # fmt: skip
DOC_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".md", ".markdown", ".mdx", ".rst", ".txt", ".adoc", ".asciidoc"}
)
DOC_DIRS: Final[tuple[str, ...]] = ("docs/", "doc/", ".github/issue_template/")
DOC_BASENAME_PREFIXES: Final[tuple[str, ...]] = (
    "license", "licence", "changelog", "changes", "authors", "notice", "codeowners",
    "contributing", "code_of_conduct",
)  # fmt: skip
ASSET_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg", ".tiff",
        ".woff", ".woff2", ".ttf", ".otf", ".eot", ".pdf", ".zip", ".gz", ".tar", ".tgz",
        ".7z", ".mp3", ".mp4", ".mov", ".wav", ".pyc", ".so", ".dll", ".exe", ".bin",
        ".parquet", ".sqlite", ".db",
    }
)  # fmt: skip
GENERATED_DIRS: Final[tuple[str, ...]] = (
    "vendor/", "node_modules/", "dist/", "build/", "__pycache__/", ".venv/", "site-packages/",
)
GENERATED_SUFFIXES: Final[tuple[str, ...]] = (
    ".min.js", ".min.css", ".map", "_pb2.py", "_pb2_grpc.py", "_pb2.pyi", ".pb.go",
)  # fmt: skip
GENERATED_MARKERS: Final[tuple[str, ...]] = ("@generated", "do not edit", "code generated")
GENERATED_HEADER_LINES: Final[int] = 5

EXTENSION_LANGUAGE: Final[Mapping[str, str]] = {".py": "python"}
"""Extension -> language. v1 is Python-only; add entries here when a grammar is registered."""

_IMPORT_RE: Final[re.Pattern[str]] = re.compile(r"^\s*(import\s+\S|from\s+\S+\s+import\s+\S)")
_WS_RE: Final[re.Pattern[str]] = re.compile(r"\s+")
_PUNCT_RE: Final[re.Pattern[str]] = re.compile(r"\s*([^\w\s])\s*")


# ---- Models -------------------------------------------------------------------------------
class PullRequestMeta(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: str
    draft: bool = False
    author_login: str = ""
    author_type: str = "User"
    labels: frozenset[str] = frozenset()


class SourcePair(NamedTuple):
    """Full file contents before/after the PR (None if unavailable)."""

    base: str | None
    head: str | None


class PreFilterResult(BaseModel):
    pr_skip: SkipReason | None = None
    kept: list[FileDiff] = Field(default_factory=list)
    skipped: dict[str, SkipReason] = Field(default_factory=dict)
    kept_changed_lines: int = 0
    truncated: bool = False

    @property
    def has_work(self) -> bool:
        return self.pr_skip is None and bool(self.kept)


# ---- PR-level gate ------------------------------------------------------------------------
def check_event(
    meta: PullRequestMeta,
    *,
    allowed_actions: Iterable[str] = ALLOWED_ACTIONS,
    skip_label: str = SKIP_LABEL,
) -> SkipReason | None:
    if meta.action not in set(allowed_actions):
        return SkipReason.UNSUPPORTED_ACTION
    if meta.draft:
        return SkipReason.DRAFT
    if meta.author_type.lower() == "bot" or meta.author_login.lower().endswith("[bot]"):
        return SkipReason.BOT_AUTHOR
    if skip_label.lower() in {label.lower() for label in meta.labels}:
        return SkipReason.SKIP_LABEL
    return None


# ---- Path rules ---------------------------------------------------------------------------
def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _extension(path: str) -> str:
    name = _basename(path).lower()
    return "." + name.rsplit(".", 1)[-1] if "." in name else ""


def language_for_path(path: str) -> str | None:
    return EXTENSION_LANGUAGE.get(_extension(path))


def classify_path(path: str) -> SkipReason | None:
    """Skip reason derivable from the file path alone (no content needed)."""
    norm = path.replace("\\", "/").lstrip("./").lower() if path.startswith("./") else path.lower()
    norm = norm.replace("\\", "/")
    name = _basename(norm)
    ext = _extension(norm)
    padded = "/" + norm  # lets "docs/" match both "docs/x" and "pkg/docs/x"

    if name in LOCKFILE_NAMES:
        return SkipReason.LOCKFILE
    if (
        ext in DOC_EXTENSIONS
        or any("/" + d in padded for d in DOC_DIRS)
        or name.startswith(DOC_BASENAME_PREFIXES)
    ):
        return SkipReason.DOCS
    if ext in ASSET_EXTENSIONS:
        return SkipReason.ASSET
    if any("/" + d in padded for d in GENERATED_DIRS) or name.endswith(GENERATED_SUFFIXES):
        return SkipReason.GENERATED
    return None


# ---- Content rules ------------------------------------------------------------------------
def has_generated_header(fd: FileDiff) -> bool:
    """True if one of the first lines of the new file carries a generated-code marker."""
    for hunk in fd.hunks:
        if hunk.target_start > GENERATED_HEADER_LINES:
            continue
        for line in hunk.lines:
            if line.target_line is None or line.target_line > GENERATED_HEADER_LINES:
                continue
            lowered = line.text.lower()
            if any(marker in lowered for marker in GENERATED_MARKERS):
                return True
    return False


def _norm_line(line: str) -> tuple[str, str] | None:
    """(indent, normalised body); None for blank and comment-only lines."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    indent = line[: len(line) - len(line.lstrip())]
    body = _PUNCT_RE.sub(r"\1", _WS_RE.sub(" ", stripped))
    return indent, body


def _norm_lines(lines: Iterable[str]) -> list[tuple[str, str]]:
    return [n for n in (_norm_line(x) for x in lines) if n is not None]


def _hunk_is_formatting_only(hunk: Hunk) -> bool:
    return _norm_lines(hunk.removed) == _norm_lines(hunk.added)


def _strip_docstrings(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            first = body[0] if body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]


def python_ast_fingerprint(source: str) -> str | None:
    """Docstring-insensitive AST dump, or None if the source does not parse.

    ``ast.parse`` never executes code, so this is safe on untrusted input.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    _strip_docstrings(tree)
    return ast.dump(tree)


def is_formatting_only(fd: FileDiff, sources: SourcePair | None = None) -> bool:
    """True when the diff cannot change program behaviour (see module docstring)."""
    if not fd.hunks:
        return False
    if (
        sources
        and sources.base is not None
        and sources.head is not None
        and language_for_path(fd.path) == "python"
    ):
        before = python_ast_fingerprint(sources.base)
        after = python_ast_fingerprint(sources.head)
        if before is not None and after is not None:
            return before == after  # authoritative
    return all(_hunk_is_formatting_only(h) for h in fd.hunks)


def is_import_reorder_only(fd: FileDiff) -> bool:
    """True when every changed line is a single-line import and only the order changed."""
    removed = [x for x in fd.removed_lines if x.strip()]
    added = [x for x in fd.added_lines if x.strip()]
    if not removed and not added:
        return False
    if not all(_IMPORT_RE.match(x) for x in (*removed, *added)):
        return False
    return sorted(_WS_RE.sub(" ", x.strip()) for x in removed) == sorted(
        _WS_RE.sub(" ", x.strip()) for x in added
    )


def classify_file(
    fd: FileDiff,
    sources: SourcePair | None = None,
    *,
    supported_extensions: Iterable[str] | None = None,
    skip_pure_deletions: bool = True,
) -> SkipReason | None:
    """Return why ``fd`` can be skipped, or None if it deserves review."""
    reason = classify_path(fd.path)
    if reason is not None:
        return reason
    if fd.is_binary:
        return SkipReason.BINARY
    allowed = (
        frozenset(supported_extensions)
        if supported_extensions is not None
        else frozenset(EXTENSION_LANGUAGE)
    )
    if _extension(fd.path) not in allowed:
        return SkipReason.UNSUPPORTED_LANGUAGE
    if fd.status is FileStatus.DELETED:
        return SkipReason.DELETED_ONLY
    if not fd.hunks:
        return SkipReason.NO_CHANGES
    if has_generated_header(fd):
        return SkipReason.GENERATED
    if skip_pure_deletions and not fd.added_lines and fd.removed_lines:
        return SkipReason.DELETED_ONLY
    if is_formatting_only(fd, sources):
        return SkipReason.FORMATTING_ONLY
    if is_import_reorder_only(fd):
        return SkipReason.TRIVIAL
    return None


def run_prefilter(
    meta: PullRequestMeta,
    files: Iterable[FileDiff],
    sources: Mapping[str, SourcePair] | None = None,
    *,
    supported_extensions: Iterable[str] | None = None,
    max_changed_lines: int = 1500,
    skip_pure_deletions: bool = True,
) -> PreFilterResult:
    """Apply the full zero-LLM pipeline to one pull request."""
    pr_skip = check_event(meta)
    if pr_skip is not None:
        return PreFilterResult(pr_skip=pr_skip)

    kept: list[FileDiff] = []
    skipped: dict[str, SkipReason] = {}
    for fd in files:
        file_reason = classify_file(
            fd,
            (sources or {}).get(fd.path),
            supported_extensions=supported_extensions,
            skip_pure_deletions=skip_pure_deletions,
        )
        if file_reason is None:
            kept.append(fd)
        else:
            skipped[fd.path] = file_reason

    truncated = False
    if sum(f.changed_line_count for f in kept) > max_changed_lines:
        truncated = True
        budget = max_changed_lines
        selected: list[FileDiff] = []
        # Smallest files first so as many files as possible are reviewed in full.
        for fd in sorted(kept, key=lambda f: (f.changed_line_count, f.path)):
            if fd.changed_line_count <= budget:
                selected.append(fd)
                budget -= fd.changed_line_count
            else:
                skipped[fd.path] = SkipReason.TOO_LARGE
        order = {f.path: i for i, f in enumerate(kept)}
        kept = sorted(selected, key=lambda f: order[f.path])

    return PreFilterResult(
        kept=kept,
        skipped=skipped,
        kept_changed_lines=sum(f.changed_line_count for f in kept),
        truncated=truncated,
    )
