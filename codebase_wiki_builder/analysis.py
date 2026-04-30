"""Analysis command for Codebase Wiki Builder.

Reads all summary files from the vault, batches them by directory tree using
tiktoken (ANALYSIS_CONTEXT_WINDOW token limit), sends each batch to the LLM
to produce partial overviews, writes per-directory overview.md files,
synthesizes a unified root overview.md, updates index.md, and appends a log
entry.

Public API used by the CLI and by the lint health-check (item 16):
  - ANALYSIS_CONTEXT_WINDOW: int constant (64,000 tokens)
  - collect_summary_files(vault_root): list of (vault_relative_dir, Path)
  - build_batches(summary_files, vault_root, logger): list[AnalysisBatch]
  - run_analysis(vault_root, llm_client, config, logger, log_fn): None
"""

from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from codebase_wiki_builder.vault import VAULT_EXCLUDED_DIRS, VAULT_SPECIAL_FILES

if TYPE_CHECKING:
    from codebase_wiki_builder.config import WikiConfig
    from codebase_wiki_builder.llm_client import LLMClient

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

ANALYSIS_CONTEXT_WINDOW = 64_000  # tokens; hardcoded per spec


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class AnalysisBatch:
    """One batch to send to the LLM for a partial overview."""

    vault_dir: str          # vault-relative POSIX dir string (e.g. "src/auth")
    file_paths: list[Path]  # absolute paths of summary files in this batch
    contents: list[str]     # file contents (parallel to file_paths)
    token_count: int        # estimated token count for the combined content


# ---------------------------------------------------------------------------
# Prompt builders — f-string functions (NEVER use .format() on prompt strings)
# ---------------------------------------------------------------------------

def _build_partial_overview_prompt(vault_dir_label: str, combined_summaries: str) -> str:
    """Build the partial overview prompt using an f-string.

    Uses an f-string rather than a template string with .format() so that curly
    braces in untrusted content (LLM-generated summary text or source file
    content) cannot corrupt the prompt or raise KeyError at the Python layer.
    """
    return (
        "You are analyzing a subset of summary files from a codebase wiki. "
        f"The files below come from the directory: {vault_dir_label}\n"
        "\n"
        "Produce a concise overview (3–8 paragraphs) covering:\n"
        "- The apparent purpose of code in this directory\n"
        "- Dominant software engineering patterns observed\n"
        "- Consistency or inconsistency in the code\n"
        "- Any notable observations or potential issues\n"
        "\n"
        "Do not fabricate details not present in the summaries below.\n"
        "\n"
        "--- SUMMARIES ---\n"
        f"{combined_summaries}\n"
        "--- END SUMMARIES ---\n"
    )


def _build_synthesis_prompt(partial_overviews_text: str) -> str:
    """Build the synthesis prompt using an f-string.

    Uses an f-string rather than a template string with .format() so that curly
    braces in untrusted LLM-generated partial overview text cannot corrupt the
    prompt or raise KeyError at the Python layer.
    """
    return (
        "You are synthesizing directory-level overviews into a unified top-level overview "
        "of an entire codebase. Each section below is an overview of one directory.\n"
        "\n"
        "Produce a comprehensive overview (5–10 paragraphs) covering:\n"
        "- The overall apparent purpose of the application\n"
        "- Dominant software engineering patterns observed across the codebase\n"
        "- Consistency or inconsistency across modules\n"
        "- Any notable observations or potential issues\n"
        "\n"
        "--- DIRECTORY OVERVIEWS ---\n"
        f"{partial_overviews_text}\n"
        "--- END DIRECTORY OVERVIEWS ---\n"
    )


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------

def _count_tokens(text: str, encoding_name: str = "cl100k_base") -> int:
    """Count tokens in text using tiktoken.

    The encoding cl100k_base is used by the GPT-4 family and is a good-enough
    approximation for Anthropic models. The 64,000-token window provides
    substantial headroom for any over-estimation.
    """
    import tiktoken
    enc = tiktoken.get_encoding(encoding_name)
    return len(enc.encode(text))


# ---------------------------------------------------------------------------
# Step 2: Stale-row detection
# ---------------------------------------------------------------------------

_TABLE_ROW_RE = re.compile(r"^\|\s*(\[\[.*?\]\])\s*\|\s*(.*?)\s*\|$")
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def _check_stale_rows(index_path: Path) -> list[str]:
    """Return vault-relative paths of stale query pages found in index.md."""
    stale_pages: list[str] = []
    try:
        content = index_path.read_text(encoding="utf-8")
    except OSError:
        return stale_pages

    for line in content.splitlines():
        m = _TABLE_ROW_RE.match(line.strip())
        if m and "⚠ stale" in m.group(2):
            inner = _WIKILINK_RE.search(m.group(1))
            if inner:
                stale_pages.append(inner.group(1) + ".md")
    return stale_pages


# ---------------------------------------------------------------------------
# Step 3: Collect summary files (public — imported by item 16)
# ---------------------------------------------------------------------------

def collect_summary_files(vault_root: Path) -> list[tuple[str, Path]]:
    """Return (vault_relative_dir_posix, absolute_path) for each summary file.

    vault_relative_dir_posix is the POSIX string of the directory relative to
    vault_root, e.g. "src/auth" for vault_root/src/auth/login.py.md.
    Root-level files have vault_relative_dir_posix = "".

    Applies the same exclusion rules used by index_writer.py:
    - Excluded dirs: logs/, queries/ (VAULT_EXCLUDED_DIRS)
    - Excluded filenames: index.md, log.md, overview.md, lint-report.md
      (VAULT_SPECIAL_FILES), plus overview.md (also in VAULT_SPECIAL_FILES)
    """
    results: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(vault_root):
        # Prune excluded dirs in-place (affects os.walk recursion)
        dirnames[:] = [d for d in dirnames if d not in VAULT_EXCLUDED_DIRS]

        current_dir = Path(dirpath)
        try:
            rel_dir = current_dir.relative_to(vault_root)
        except ValueError:
            continue

        rel_dir_posix = rel_dir.as_posix() if rel_dir != Path(".") else ""

        for filename in filenames:
            if not filename.endswith(".md"):
                continue
            if filename in VAULT_SPECIAL_FILES:
                continue
            if filename == "overview.md":
                # overview.md is already in VAULT_SPECIAL_FILES, but be explicit
                continue
            results.append((rel_dir_posix, current_dir / filename))

    return results


# ---------------------------------------------------------------------------
# Step 4: Tiktoken batching (public — imported by item 16)
# ---------------------------------------------------------------------------

def _immediate_subdir(rel_dir: str, parent_dir: str) -> str:
    """Return the key one level below parent_dir for rel_dir.

    Examples:
      parent="src", rel_dir="src/auth/utils" -> "src/auth"
      parent="", rel_dir="src" -> "src"
      parent="", rel_dir="" -> "" (file at root stays at root)
    """
    if parent_dir == "":
        # Split on first "/" to get immediate child
        parts = rel_dir.split("/", 1)
        return parts[0]
    # Strip parent_dir prefix and take next segment
    suffix = rel_dir[len(parent_dir):].lstrip("/")
    if not suffix:
        return parent_dir
    next_segment = suffix.split("/")[0]
    return f"{parent_dir}/{next_segment}" if parent_dir else next_segment


def _subdivide_into_batches(
    files: list[tuple[str, Path]],
    group_dir: str,
    vault_root: Path,
    batches: list[AnalysisBatch],
    logger: logging.Logger,
) -> None:
    """Recursively subdivide files into batches that fit the context window.

    Strategy (per FR-4):
    1. If a group fits in ANALYSIS_CONTEXT_WINDOW -> one batch.
    2. If a group exceeds the window -> subdivide into immediate subdirectories
       and repeat until each subdivision fits.
    3. Continue recursively until each batch fits or a single file is
       irreducibly too large (include it alone with a warning).
    """
    if not files:
        return

    # Load file contents
    contents: list[str] = []
    paths: list[Path] = []
    for _, path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Cannot read summary file %s: %s", path, exc)
            text = ""
        contents.append(text)
        paths.append(path)

    combined_text = "\n\n".join(contents)
    total_tokens = _count_tokens(combined_text)

    if total_tokens <= ANALYSIS_CONTEXT_WINDOW:
        # Fits: create one batch for this directory group
        batches.append(AnalysisBatch(
            vault_dir=group_dir,
            file_paths=paths,
            contents=contents,
            token_count=total_tokens,
        ))
        return

    # Too large: subdivide by immediate subdirectory
    sub_groups: dict[str, list[tuple[str, Path]]] = defaultdict(list)
    for rel_dir, path in files:
        sub_key = _immediate_subdir(rel_dir, group_dir)
        sub_groups[sub_key].append((rel_dir, path))

    if len(sub_groups) == 1:
        # Cannot subdivide further (all files in same dir, or single file)
        # Include as one batch anyway; LLM call may exceed window but this
        # is unavoidable. Log a warning.
        logger.warning(
            "Batch for '%s' exceeds context window (%d tokens) but cannot be "
            "subdivided further. Sending anyway.",
            group_dir,
            total_tokens,
        )
        batches.append(AnalysisBatch(
            vault_dir=group_dir,
            file_paths=paths,
            contents=contents,
            token_count=total_tokens,
        ))
        return

    for sub_dir, sub_files in sorted(sub_groups.items()):
        _subdivide_into_batches(sub_files, sub_dir, vault_root, batches, logger)


def build_batches(
    summary_files: list[tuple[str, Path]],
    vault_root: Path,
    logger: logging.Logger,
) -> list[AnalysisBatch]:
    """Group summary files into batches by directory.

    Strategy (per FR-4):
    1. Group files by their top-level vault directory (first path segment).
    2. If a group fits in ANALYSIS_CONTEXT_WINDOW -> one batch.
    3. If a group exceeds the window -> subdivide into immediate subdirectories
       and repeat until each subdivision fits.
    4. Continue recursively until each batch fits or a single file is
       irreducibly too large (include it alone with a warning).

    This is a public function imported by item 16 (lint health-check).
    """
    def top_level_dir(rel_dir: str) -> str:
        if not rel_dir:
            return ""
        return rel_dir.split("/")[0]

    top_groups: dict[str, list[tuple[str, Path]]] = defaultdict(list)
    for rel_dir, path in summary_files:
        top_groups[top_level_dir(rel_dir)].append((rel_dir, path))

    batches: list[AnalysisBatch] = []
    for top_dir, group in sorted(top_groups.items()):
        _subdivide_into_batches(group, top_dir, vault_root, batches, logger)

    return batches


# ---------------------------------------------------------------------------
# Steps 5 & 6: LLM call per batch -> write per-directory overview.md
# ---------------------------------------------------------------------------

def _process_batch(
    batch: AnalysisBatch,
    vault_root: Path,
    llm_client: LLMClient,
    logger: logging.Logger,
) -> str:
    """Send batch to LLM and return partial overview text. Writes overview.md.

    Raises LLMError on fatal LLM failures — the CLI catches this.
    """
    vault_dir_label = batch.vault_dir if batch.vault_dir else "(root)"
    logger.info(
        "Processing batch for '%s': %d files, ~%d tokens",
        vault_dir_label,
        len(batch.file_paths),
        batch.token_count,
    )

    combined = "\n\n---\n\n".join(
        f"File: {path.name}\n\n{content}"
        for path, content in zip(batch.file_paths, batch.contents)
    )
    prompt = _build_partial_overview_prompt(vault_dir_label, combined)

    overview_text = llm_client.complete(prompt)  # raises LLMError on fatal failure

    # Write per-directory overview.md
    if batch.vault_dir:
        overview_dir = vault_root / Path(batch.vault_dir)
    else:
        # Root-level files: overview goes at vault root
        # (synthesis step will overwrite with the synthesized result)
        overview_dir = vault_root
    overview_dir.mkdir(parents=True, exist_ok=True)
    overview_path = overview_dir / "overview.md"
    overview_path.write_text(overview_text, encoding="utf-8")
    logger.info("Wrote overview.md to %s", overview_path)

    return overview_text


# ---------------------------------------------------------------------------
# Step 7: Synthesize root overview.md
# ---------------------------------------------------------------------------

def _synthesize_root_overview(
    partial_overviews: list[tuple[str, str]],  # (vault_dir, overview_text)
    vault_root: Path,
    llm_client: LLMClient,
    logger: logging.Logger,
) -> None:
    """Synthesize all partial overviews into root overview.md.

    Raises LLMError on fatal LLM failures — the CLI catches this.
    """
    if not partial_overviews:
        logger.warning("No partial overviews to synthesize; root overview.md will be empty.")
        return

    combined_sections = "\n\n---\n\n".join(
        f"Directory: {vault_dir if vault_dir else '(root)'}\n\n{text}"
        for vault_dir, text in partial_overviews
    )
    prompt = _build_synthesis_prompt(combined_sections)

    root_overview = llm_client.complete(prompt)

    root_overview_path = vault_root / "overview.md"
    root_overview_path.write_text(root_overview, encoding="utf-8")
    logger.info("Wrote root overview.md")


# ---------------------------------------------------------------------------
# Step 8: Update index.md with overview rows
# ---------------------------------------------------------------------------

def _update_index_with_overviews(
    vault_root: Path,
    written_overview_paths: list[Path],
    logger: logging.Logger,
) -> None:
    """Add or update overview.md rows in index.md.

    For each written overview, compute its wikilink and description.
    If a row for that wikilink already exists in index.md, update its
    description. If no row exists, append a new row.

    Does NOT call rebuild_index() — performs targeted row updates/appends
    to avoid disturbing existing summary and query page rows.
    """
    from codebase_wiki_builder.vault import wikilink as make_wikilink

    index_path = vault_root / "index.md"
    if not index_path.exists():
        logger.warning("index.md not found; cannot update with overview entries")
        return

    try:
        content = index_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Cannot read index.md: %s", exc)
        return

    lines = content.splitlines(keepends=True)

    for overview_path in written_overview_paths:
        link = make_wikilink(overview_path, vault_root)
        # Determine description based on location
        if overview_path.parent == vault_root:
            desc = "Top-level application overview"
        else:
            rel_dir = overview_path.parent.relative_to(vault_root)
            desc = f"Directory overview: {rel_dir.as_posix()}/"

        # Compute the link target (path without .md extension, POSIX)
        link_target = overview_path.relative_to(vault_root).with_suffix("").as_posix()
        row_exists = any(f"[[{link_target}]]" in line for line in lines)

        new_row = f"| {link} | {desc} |\n"
        if row_exists:
            # Replace existing row in-place
            lines = [
                new_row if f"[[{link_target}]]" in line else line
                for line in lines
            ]
        else:
            # Append new row at end of file
            lines.append(new_row)

    try:
        index_path.write_text("".join(lines), encoding="utf-8")
        logger.info(
            "Updated index.md with %d overview entries", len(written_overview_paths)
        )
    except OSError as exc:
        logger.error("Cannot write updated index.md: %s", exc)


# ---------------------------------------------------------------------------
# Step 9: Append analysis log entry
# ---------------------------------------------------------------------------

def _write_analysis_log_entry(
    vault_root: Path,
    summary_count: int,
    log_fn: Callable[[str], None],
) -> None:
    """Append a log entry to log.md for this analysis run."""
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = f"{ts} | analysis | summaries_reviewed={summary_count}"
    log_fn(entry)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_analysis(
    vault_root: Path,
    llm_client: LLMClient,
    config: WikiConfig,
    logger: logging.Logger,
    log_fn: Callable[[str], None],
) -> None:
    """Run the full analysis workflow.

    Steps:
      1. Check index.md exists (hard error + typer.Exit(1) if absent)
      2. Scan index.md for stale rows; print warning if any found
      3. Collect all summary files from vault
      4. Batch summaries by directory tree using tiktoken
      5. For each batch, send to LLM -> partial overview string
      6. Write per-directory overview.md files
      7. Synthesize all partial overviews into root overview.md
      8. Update index.md with all overview.md entries
      9. Append analysis log entry to log.md

    Raises LLMError on fatal LLM failures (the CLI catches and converts to
    sys.exit(1)).
    """
    import typer

    # Step 1: Empty-vault guard
    index_path = vault_root / "index.md"
    if not index_path.exists():
        typer.echo(
            "The vault has no summaries. Run 'codewiki ingest' first.", err=True
        )
        raise typer.Exit(code=1)

    # Step 2: Stale-row warning (informational only — never blocks analysis)
    stale_pages = _check_stale_rows(index_path)
    if stale_pages:
        count = len(stale_pages)
        names = ", ".join(stale_pages)
        typer.echo(
            f"⚠ {count} query page(s) are stale: {names} "
            f"— run codewiki lint to update."
        )

    # Step 3: Collect summary files
    summary_files = collect_summary_files(vault_root)
    logger.info("Found %d summary files for analysis", len(summary_files))

    if not summary_files:
        typer.echo("No summary files found. Run 'codewiki ingest' first.", err=True)
        raise typer.Exit(code=1)

    # Step 4: Build tiktoken batches
    batches = build_batches(summary_files, vault_root, logger)
    logger.info("Built %d batch(es) for analysis", len(batches))

    # Steps 5 & 6: Process each batch -> partial overview -> write per-dir overview.md
    partial_overviews: list[tuple[str, str]] = []  # (vault_dir, text)
    written_overview_paths: list[Path] = []

    for batch in batches:
        overview_text = _process_batch(batch, vault_root, llm_client, logger)
        partial_overviews.append((batch.vault_dir, overview_text))

        if batch.vault_dir:
            overview_path = vault_root / Path(batch.vault_dir) / "overview.md"
        else:
            overview_path = vault_root / "overview.md"
        written_overview_paths.append(overview_path)

    # Step 7: Synthesize root overview.md
    # Synthesis always runs, even with a single batch — keeps code path uniform.
    # If there's only one root-level batch, synthesis produces the definitive
    # root overview.md (overwriting the intermediate write from _process_batch).
    _synthesize_root_overview(partial_overviews, vault_root, llm_client, logger)
    root_overview_path = vault_root / "overview.md"
    # Ensure root overview.md is in the written list (synthesis always writes it)
    if root_overview_path not in written_overview_paths:
        written_overview_paths.append(root_overview_path)

    # Step 8: Update index.md with overview entries
    _update_index_with_overviews(vault_root, written_overview_paths, logger)

    # Step 9: Log entry
    _write_analysis_log_entry(vault_root, len(summary_files), log_fn)

    typer.echo(
        f"Analysis complete. Reviewed {len(summary_files)} summaries across "
        f"{len(batches)} batch(es). Root overview.md written."
    )
