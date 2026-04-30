"""Staleness resolution for Codebase Wiki Builder lint command.

Implements resolve_stale_pages(), which re-runs the full query workflow for
each stale query page found in index.md, overwrites the page with a fresh
answer, and cleans up stale banner annotations.

Pages where no relevant files can be found (NoRelevantFilesError) are flagged
as "unknowable" rather than regenerated.

Public API:
  - LintStalenessResult: dataclass with resolved, unknowable, and skipped page lists
  - resolve_stale_pages(): main entry point for lint Part 1 staleness resolution

No typer imports — this is a pure logic module. The lint CLI (item 17) handles
all framework concerns.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from rich.console import Console

if TYPE_CHECKING:
    from codebase_wiki_builder.config import WikiConfig
    from codebase_wiki_builder.llm_client import LLMClient
    from codebase_wiki_builder.query_engine import QueryResult
    from codebase_wiki_builder.query_persistence import QueryPage

logger = logging.getLogger(__name__)
_console = Console()

# ---------------------------------------------------------------------------
# Module-level regex constants
# ---------------------------------------------------------------------------

_STALE_BANNER_START_RE = re.compile(r"^> \[!warning\] Stale Content\s*$")
_STALE_ROW_RE = re.compile(r"\[\[([^\]]+)\]\].*⚠ stale")
_INDEX_ROW_RE = re.compile(r"^(\|\s*\[\[[^\]]+\]\]\s*\|\s*)(.+?)(\s*\|)\s*$")

# ---------------------------------------------------------------------------
# Unknowable banner text
# ---------------------------------------------------------------------------

_UNKNOWABLE_BANNER = """> [!error] Unknowable
> This question cannot be answered by the current wiki or codebase.
> Run `codewiki ingest` then `codewiki lint` if the codebase has changed."""


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------

@dataclass
class LintStalenessResult:
    """Result of a resolve_stale_pages() run."""

    resolved_pages: list[Path] = field(default_factory=list)
    """Pages successfully regenerated with fresh answers."""

    unknowable_pages: list[Path] = field(default_factory=list)
    """Pages where re-run returned zero relevant files."""

    skipped_pages: list[Path] = field(default_factory=list)
    """Pages that could not be processed due to read/write errors (not aborted)."""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def resolve_stale_pages(
    vault_root: Path,
    llm_client: "LLMClient",
    config: "WikiConfig",
    log_fn: Callable[[str], None],
) -> LintStalenessResult:
    """Resolve all stale query pages found in index.md.

    Steps:
      1. Read index.md; collect stale page paths.
      2. If none: print "No stale query pages found." and return empty result.
      3. For each stale page (in index.md row order):
         a. Strip ALL stale banners from the file.
         b. Re-run the full query workflow (logged as lint-query).
         c. Handle unknowable case (zero relevant files).
         d. Overwrite page with fresh answer; update updated_at; preserve saved_at.
         e. Remove stale annotation from index.md row.
         f. Log lint-resolved or lint-unknowable.
         g. Print per-page terminal output.
      4. Print staleness resolved summary.
      5. Return LintStalenessResult.

    Args:
        vault_root: Absolute path to the vault root directory.
        llm_client: Configured LLM client for running queries.
        config: Wiki configuration.
        log_fn: Callable that accepts a pre-formatted log entry string and
                appends it to log.md. Constructed by the lint CLI as
                partial(append_log_md, vault_root).

    Returns:
        LintStalenessResult with categorised page lists.
    """
    from codebase_wiki_builder.query_persistence import read_query_page

    stale_paths = _collect_stale_pages(vault_root)

    if not stale_paths:
        _console.print("No stale query pages found.")
        return LintStalenessResult(
            resolved_pages=[], unknowable_pages=[], skipped_pages=[]
        )

    resolved: list[Path] = []
    unknowable: list[Path] = []
    skipped: list[Path] = []

    for page_path in stale_paths:
        if not page_path.exists():
            logger.warning(
                "Stale page listed in index.md not found on disk: %s", page_path
            )
            skipped.append(page_path)
            continue

        try:
            page = read_query_page(page_path)
        except (OSError, ValueError) as exc:
            logger.error("Cannot read stale page %s: %s", page_path, exc)
            skipped.append(page_path)
            continue

        # Step a: Strip all stale banners (in memory)
        # Note: page.question is already parsed from the H1; we use it directly
        question = page.question

        # Step b: Log lint-query entry, then re-run query
        _log_lint_query(page_path, vault_root, log_fn)
        result = _run_internal_query(question, vault_root, llm_client, config)

        timestamp = _utc_now()

        if result is None:
            # Step c: Unknowable case — zero relevant files
            new_content = _build_unknowable_page(page, timestamp)
            try:
                page_path.write_text(new_content, encoding="utf-8")
            except OSError as exc:
                logger.error(
                    "Cannot write unknowable page %s: %s", page_path, exc
                )
                skipped.append(page_path)
                continue

            _update_index_row(
                vault_root, page_path, None, unknowable=True, log=logger
            )
            _log_lint_unknowable(page_path, vault_root, log_fn)
            _print_unknowable(page_path, vault_root)
            unknowable.append(page_path)

        else:
            # Steps d–g: Resolved case
            new_content = _build_resolved_page(question, result, page, timestamp)
            try:
                page_path.write_text(new_content, encoding="utf-8")
            except OSError as exc:
                logger.error(
                    "Cannot write resolved page %s: %s", page_path, exc
                )
                skipped.append(page_path)
                continue

            _update_index_row(
                vault_root,
                page_path,
                result.one_line_summary,
                unknowable=False,
                log=logger,
            )
            _log_lint_resolved(page_path, vault_root, log_fn)
            _print_resolved(page_path, vault_root)
            resolved.append(page_path)

    # Final summary — counts only resolved pages (not unknowable)
    _console.print(f"Staleness resolved: {len(resolved)} page(s) updated.")

    return LintStalenessResult(
        resolved_pages=resolved,
        unknowable_pages=unknowable,
        skipped_pages=skipped,
    )


# ---------------------------------------------------------------------------
# Internal helpers — stale page collection
# ---------------------------------------------------------------------------

def _collect_stale_pages(vault_root: Path) -> list[Path]:
    """Return absolute paths of stale query pages from index.md, in row order.

    Returns an empty list if index.md does not exist or has no stale rows.
    Wikilink targets omit the .md extension; this function adds it back.
    """
    index_path = vault_root / "index.md"
    if not index_path.exists():
        return []

    stale_paths: list[Path] = []
    for line in index_path.read_text(encoding="utf-8").splitlines():
        m = _STALE_ROW_RE.search(line)
        if m:
            # Wikilink target omits .md extension; add it back
            rel = m.group(1) + ".md"
            stale_paths.append(vault_root / rel)
    return stale_paths


# ---------------------------------------------------------------------------
# Internal helpers — banner stripping
# ---------------------------------------------------------------------------

def _strip_stale_banners(content: str) -> str:
    """Remove ALL stale banner blocks from content.

    A stale banner block starts with a line matching:
        > [!warning] Stale Content
    and includes all immediately following lines that start with '>'.
    Blank lines between '>' lines do NOT continue the block.
    Also removes the blank line immediately preceding the block if one exists.
    """
    lines = content.splitlines(keepends=True)
    result: list[str] = []
    i = 0

    while i < len(lines):
        stripped = lines[i].rstrip("\n").rstrip()
        if _STALE_BANNER_START_RE.match(stripped):
            # Remove this line and all immediately following '>' lines
            i += 1
            while i < len(lines) and lines[i].startswith(">"):
                i += 1
            # Consume one trailing blank line after the block (if present)
            if i < len(lines) and lines[i].strip() == "":
                i += 1
            # Remove the blank line we may have already appended before the block
            if result and result[-1].strip() == "":
                result.pop()
        else:
            result.append(lines[i])
            i += 1

    return "".join(result)


# ---------------------------------------------------------------------------
# Internal helpers — query re-run
# ---------------------------------------------------------------------------

def _run_internal_query(
    question: str,
    vault_root: Path,
    llm_client: "LLMClient",
    config: "WikiConfig",
) -> "QueryResult | None":
    """Run the query workflow for lint. Returns None if zero relevant files found.

    Catches NoRelevantFilesError to enter the unknowable branch.
    Re-raises FileNotFoundError (index missing — should not occur if the lint
    CLI verified index.md before calling resolve_stale_pages()).
    LLMError propagates to the lint CLI as a fatal error.
    """
    from codebase_wiki_builder.query_engine import run_query, NoRelevantFilesError

    try:
        return run_query(question, vault_root, llm_client, config)
    except NoRelevantFilesError:
        return None  # unknowable: zero relevant files found


# ---------------------------------------------------------------------------
# Internal helpers — page content builders
# ---------------------------------------------------------------------------

def _build_unknowable_page(page: "QueryPage", timestamp: str) -> str:
    """Rebuild the page content for an unknowable page.

    Structure:
        # <question>

        > [!error] Unknowable
        > This question cannot be answered...
        > Run `codewiki ingest` then `codewiki lint` if the codebase has changed.

        this question cannot be answered by the wiki or the codebase

        ## Sources
        <original sources preserved>

        ## Page Metadata
        saved_at: <original>
        updated_at: <now>
    """
    parts = [
        f"# {page.question}",
        "",
        _UNKNOWABLE_BANNER,
        "",
        "this question cannot be answered by the wiki or the codebase",
        "",
        "## Sources",
    ]
    for src in page.sources:
        parts.append(f"- {src}")
    parts += [
        "",
        "## Page Metadata",
        f"saved_at: {page.saved_at}",
        f"updated_at: {timestamp}",
        "",
    ]
    return "\n".join(parts)


def _build_resolved_page(
    question: str,
    result: "QueryResult",
    page: "QueryPage",
    timestamp: str,
) -> str:
    """Rebuild the page content with the fresh answer.

    Structure:
        # <question>

        <result.answer>  (includes answer body + ## Sources)

        ## Page Metadata
        saved_at: <original>
        updated_at: <now>
    """
    parts = [
        f"# {question}",
        "",
        result.answer,     # full answer body + ## Sources section
        "",
        "## Page Metadata",
        f"saved_at: {page.saved_at}",
        f"updated_at: {timestamp}",
        "",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Internal helpers — index.md annotation update
# ---------------------------------------------------------------------------

def _update_index_row(
    vault_root: Path,
    page_path: Path,
    new_description: str | None,
    unknowable: bool,
    log: logging.Logger,
) -> None:
    """Update the index.md row for a resolved or unknowable page.

    Args:
        vault_root: Absolute path to the vault root directory.
        page_path: Absolute path to the query page file.
        new_description: Fresh one-line summary for resolved pages. None for unknowable.
        unknowable: If True, replaces ⚠ stale with ⊘ unknowable.
                    If False, removes ⚠ stale and updates description.
        log: Module logger for error messages.
    """
    index_path = vault_root / "index.md"
    try:
        content = index_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.error("Cannot read index.md to update annotation: %s", exc)
        return

    # Build the wikilink target (vault-relative, no .md extension)
    rel = page_path.relative_to(vault_root)
    wikilink_target = rel.as_posix().removesuffix(".md")

    new_lines: list[str] = []
    for line in content.splitlines():
        if f"[[{wikilink_target}]]" in line:
            if unknowable:
                # Replace ⚠ stale with ⊘ unknowable
                line = line.replace(" ⚠ stale", " ⊘ unknowable")
            else:
                # Remove ⚠ stale annotation
                line = line.replace(" ⚠ stale", "")
                # Update description if we have a fresh one-line summary
                if new_description is not None:
                    line = _replace_description_in_row(line, new_description)
        new_lines.append(line)

    try:
        index_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    except OSError as exc:
        log.error("Cannot write updated index.md: %s", exc)


def _replace_description_in_row(line: str, new_description: str) -> str:
    """Replace the description cell in a two-column index.md table row.

    Row format: | [[wikilink]] | Description |
    """
    m = _INDEX_ROW_RE.match(line)
    if m:
        safe_desc = new_description.replace("|", "\\|")
        return f"{m.group(1)}{safe_desc}{m.group(3)}"
    # If row doesn't match expected format, leave it unchanged
    return line


# ---------------------------------------------------------------------------
# Internal helpers — logging
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    """Return the current UTC time as a formatted string."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _log_lint_query(
    page_path: Path, vault_root: Path, log_fn: Callable[[str], None]
) -> None:
    """Write a lint-query log entry (before re-running the query)."""
    ts = _utc_now()
    rel = page_path.relative_to(vault_root).as_posix()
    log_fn(f"{ts} | lint-query | {rel} (re-run for staleness resolution)")


def _log_lint_resolved(
    page_path: Path, vault_root: Path, log_fn: Callable[[str], None]
) -> None:
    """Write a lint-resolved log entry."""
    ts = _utc_now()
    rel = page_path.relative_to(vault_root).as_posix()
    log_fn(f"{ts} | lint-resolved | {rel}")


def _log_lint_unknowable(
    page_path: Path, vault_root: Path, log_fn: Callable[[str], None]
) -> None:
    """Write a lint-unknowable log entry."""
    ts = _utc_now()
    rel = page_path.relative_to(vault_root).as_posix()
    log_fn(f"{ts} | lint-unknowable | {rel}")


# ---------------------------------------------------------------------------
# Internal helpers — terminal output
# ---------------------------------------------------------------------------

def _print_resolved(page_path: Path, vault_root: Path) -> None:
    """Print a success line for a resolved page."""
    rel = page_path.relative_to(vault_root).as_posix()
    _console.print(f"[green]✓ Regenerated:[/green] {rel}")


def _print_unknowable(page_path: Path, vault_root: Path) -> None:
    """Print a dim line for an unknowable page."""
    rel = page_path.relative_to(vault_root).as_posix()
    _console.print(f"[dim]⊘ Unknowable:[/dim] {rel}")
