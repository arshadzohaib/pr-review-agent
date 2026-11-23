"""Unified-diff parsing into small immutable Pydantic models (via ``unidiff``)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict
from unidiff import PatchSet
from unidiff.errors import UnidiffParseError


class LineKind(StrEnum):
    ADDED = "+"
    REMOVED = "-"
    CONTEXT = " "


class DiffLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: LineKind
    text: str  # without the +/-/space prefix and without line terminator
    source_line: int | None = None
    target_line: int | None = None


class Hunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_start: int
    source_length: int
    target_start: int
    target_length: int
    section_header: str = ""
    lines: tuple[DiffLine, ...] = ()

    @property
    def added(self) -> list[str]:
        return [ln.text for ln in self.lines if ln.kind is LineKind.ADDED]

    @property
    def removed(self) -> list[str]:
        return [ln.text for ln in self.lines if ln.kind is LineKind.REMOVED]

    @property
    def target_end(self) -> int:
        """Last line number (inclusive) of this hunk in the new file."""
        return self.target_start + max(self.target_length, 1) - 1


class FileStatus(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


class FileDiff(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str  # path in the new tree (old path for deletions)
    old_path: str | None = None
    status: FileStatus = FileStatus.MODIFIED
    is_binary: bool = False
    hunks: tuple[Hunk, ...] = ()

    @property
    def added_lines(self) -> list[str]:
        return [t for h in self.hunks for t in h.added]

    @property
    def removed_lines(self) -> list[str]:
        return [t for h in self.hunks for t in h.removed]

    @property
    def changed_line_count(self) -> int:
        return len(self.added_lines) + len(self.removed_lines)

    @property
    def extension(self) -> str:
        name = self.path.rsplit("/", 1)[-1]
        return "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _strip_prefix(path: str) -> str:
    return path[2:] if path.startswith(("a/", "b/")) else path


def parse_diff(diff_text: str) -> list[FileDiff]:
    """Parse a unified diff (e.g. GitHub's ``.diff`` media type) into ``FileDiff`` models."""
    if not diff_text.strip():
        return []
    try:
        patch = PatchSet(diff_text)
    except UnidiffParseError as exc:
        raise ValueError(f"malformed unified diff: {exc}") from exc

    files: list[FileDiff] = []
    for pf in patch:
        if pf.is_added_file:
            status, old_path = FileStatus.ADDED, None
        elif pf.is_removed_file:
            status, old_path = FileStatus.DELETED, _strip_prefix(pf.source_file)
        elif getattr(pf, "is_rename", False):
            status, old_path = FileStatus.RENAMED, _strip_prefix(pf.source_file)
        else:
            status, old_path = FileStatus.MODIFIED, None

        hunks: list[Hunk] = []
        for h in pf:
            lines = tuple(
                DiffLine(
                    kind=LineKind(ln.line_type),
                    text=str(ln.value).rstrip("\r\n"),
                    source_line=ln.source_line_no,
                    target_line=ln.target_line_no,
                )
                for ln in h
                if ln.line_type in {"+", "-", " "}  # skip "\ No newline at end of file"
            )
            hunks.append(
                Hunk(
                    source_start=h.source_start,
                    source_length=h.source_length,
                    target_start=h.target_start,
                    target_length=h.target_length,
                    section_header=(h.section_header or "").strip(),
                    lines=lines,
                )
            )
        files.append(
            FileDiff(
                path=pf.path,
                old_path=old_path,
                status=status,
                is_binary=bool(pf.is_binary_file),
                hunks=tuple(hunks),
            )
        )
    return files
