"""Token-budgeted AST Context builder and chunk packer.

Assembles compact, deterministic context payloads for Gemini prompts in strict priority order:
1. Modified function/method full source + unified diff hunk
2. Enclosing class header & __init__ attributes
3. Direct 1-hop caller snippets (ranked by relevance)
4. Imported symbol signatures
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict, Field

from app.ast_engine.index import CallerSnippet, RepoIndex
from app.ast_engine.symbols import HunkSymbolMatch
from app.core.cache import compute_chunk_sha, normalize_function_source
from app.core.diff_parser import Hunk
from app.core.rate_limiter import estimate_tokens


class DiffChunk(BaseModel):
    """A modified hunk mapped to symbol and cache identifiers."""

    model_config = ConfigDict(frozen=True)

    file_path: str
    language: str
    symbol: str | None = None
    hunk: str
    head_start: int
    head_end: int
    chunk_sha: str


class ASTContext(BaseModel):
    """Structured, token-budgeted context for LLM prompt generation."""

    model_config = ConfigDict(frozen=True)

    chunk_sha: str
    file_path: str
    symbol_name: str
    function_source: str
    diff_hunk: str
    class_header: str | None = None
    callers: list[CallerSnippet] = Field(default_factory=list)
    imported_signatures: list[str] = Field(default_factory=list)
    est_tokens: int = 0


def format_hunk_text(hunk: Hunk) -> str:
    """Reconstruct unified diff text representation for a hunk."""
    header = (
        f"@@ -{hunk.source_start},{hunk.source_length} "
        f"+{hunk.target_start},{hunk.target_length} @@"
    )
    if hunk.section_header:
        header += f" {hunk.section_header}"
    lines = [header]
    for line in hunk.lines:
        lines.append(f"{line.kind.value}{line.text}")
    return "\n".join(lines)


def build_ast_context_for_match(
    match: HunkSymbolMatch,
    file_path: str,
    index: RepoIndex,
    language: str = "python",
    max_tokens: int = 6000,
    max_callers: int = 3,
) -> tuple[DiffChunk, ASTContext]:
    """Assemble a DiffChunk and token-budgeted ASTContext for a hunk match."""
    hunk = match.hunk
    hunk_text = format_hunk_text(hunk)

    if match.symbol is not None:
        symbol_name = match.symbol.name
        func_source = match.symbol.source_code
    else:
        symbol_name = f"module_lines_{hunk.target_start}_{hunk.target_end}"
        # Fallback to hunk added lines
        func_source = "\n".join(hunk.added) if hunk.added else hunk_text

    # 1. Enclosing class header & init attributes
    class_header: str | None = None
    if match.enclosing_class:
        cls_parts = [match.enclosing_class.signature]
        if match.enclosing_class.attributes:
            cls_parts.append("    # __init__ attributes:")
            for attr in match.enclosing_class.attributes[:5]:
                cls_parts.append(f"    {attr}")
        class_header = "\n".join(cls_parts)

    # 2. Callers
    callers: list[CallerSnippet] = []
    if match.symbol is not None:
        callers = index.find_direct_callers(match.symbol, max_callers=max_callers)

    # 3. Imported signatures from current file
    file_imports = index.imports_by_file.get(file_path, [])
    imported_sigs: list[str] = []
    for imp in file_imports:
        imported_symbols = index.symbols_by_name.get(imp.imported_name, [])
        for isym in imported_symbols:
            imported_sigs.append(f"{isym.signature} (from {isym.file_path})")

    # 4. Context Hash for caching
    ctx_hasher = hashlib.sha256()
    for c in callers:
        ctx_hasher.update(c.snippet.encode("utf-8"))
        ctx_hasher.update(b"\x00")
    ctx_hash = ctx_hasher.hexdigest()

    norm_src = normalize_function_source(func_source, lang=language)
    chunk_sha = compute_chunk_sha(norm_src, ctx_hash, lang=language)

    # 5. Token budget enforcement
    # Priority order:
    # 1: func_source + hunk_text (never cut)
    # 2: class_header
    # 3: callers
    # 4: imported_sigs
    base_text = f"{func_source}\n{hunk_text}"
    base_tokens = estimate_tokens(base_text)
    remaining_budget = max(0, max_tokens - base_tokens)

    # Add class header if budget permits
    final_class_header: str | None = None
    if class_header and estimate_tokens(class_header) <= remaining_budget:
        final_class_header = class_header
        remaining_budget -= estimate_tokens(class_header)

    # Add callers up to budget
    final_callers: list[CallerSnippet] = []
    for caller in callers:
        t = estimate_tokens(caller.snippet)
        if t <= remaining_budget:
            final_callers.append(caller)
            remaining_budget -= t
        else:
            break

    # Add imported signatures up to budget
    final_sigs: list[str] = []
    for sig in imported_sigs:
        t = estimate_tokens(sig)
        if t <= remaining_budget:
            final_sigs.append(sig)
            remaining_budget -= t
        else:
            break

    total_text = f"{base_text}\n{final_class_header or ''}\n" + "\n".join(
        c.snippet for c in final_callers
    ) + "\n".join(final_sigs)
    est_total_tokens = estimate_tokens(total_text)

    chunk = DiffChunk(
        file_path=file_path,
        language=language,
        symbol=symbol_name,
        hunk=hunk_text,
        head_start=hunk.target_start,
        head_end=hunk.target_end,
        chunk_sha=chunk_sha,
    )

    context = ASTContext(
        chunk_sha=chunk_sha,
        file_path=file_path,
        symbol_name=symbol_name,
        function_source=func_source,
        diff_hunk=hunk_text,
        class_header=final_class_header,
        callers=final_callers,
        imported_signatures=final_sigs,
        est_tokens=est_total_tokens,
    )

    return chunk, context


def pack_ast_contexts(
    contexts: list[ASTContext], max_tokens_per_batch: int = 6000
) -> list[list[ASTContext]]:
    """Pack multiple small function contexts into token-capped batches to minimize LLM calls."""
    if not contexts:
        return []

    batches: list[list[ASTContext]] = []
    current_batch: list[ASTContext] = []
    current_tokens = 0

    for ctx in contexts:
        tokens = ctx.est_tokens
        if current_batch and (current_tokens + tokens > max_tokens_per_batch):
            batches.append(current_batch)
            current_batch = [ctx]
            current_tokens = tokens
        else:
            current_batch.append(ctx)
            current_tokens += tokens

    if current_batch:
        batches.append(current_batch)

    return batches
