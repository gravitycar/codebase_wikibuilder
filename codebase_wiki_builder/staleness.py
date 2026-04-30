"""Staleness detection for Codebase Wiki Builder.

Implements detect_stale_queries(), which scans query pages under queries/,
checks each page's ## Sources section against the Phase 1 change-set, inserts
a stale callout banner when sources have changed, and annotates the index.md
row for flagged pages.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from codebase_wiki_builder.scanner import ChangeSet
from codebase_wiki_builder.vault import vault_path_for_source


# ---------------------------------------------------------------------------
# StalenessResult dataclass
# ---------------------------------------------------------------------------

@dataclass
class StalenessResult:
    # Query pages successfully flagged as stale (had sources in change-set; banner inserted)
    flagged_pages: list[Path] = field(default_factory=list)

    # Query pages already stale (banner already present; no action taken)
    already_stale_pages: list[Path] = field(default_factory=list)

    # Query pages with missing or malformed ## Sources section (hard errors)
    malformed_sources_pages: list[Path] = field(default_factory=list)

    # Query pages with no sources in the change-set (not stale)
    clean_pages: list[Path] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Module-level regex constants
# ---------------------------------------------------------------------------

_SOURCES_HEADING_RE = re.compile(r"^##\s+Sources\s*$", re.MULTILINE)
_SOURCE_ITEM_RE = re.compile(r"^\s*-\s+(\S+)", re.MULTILINE)
_STALE_BANNER_RE = re.compile(r"^>\s*\[!warning\]\s*Stale Content", re.MULTILINE)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def detect_stale_queries(
    change_set: ChangeSet,
    vault_root: Path,
    codebase_root: Path,
    log_fn: Callable[[str], None],
    logger: logging.Logger,
) -> StalenessResult:
    """Detect stale query pages and insert stale banners where needed.

    Builds the set of changed vault-relative paths internally from the raw
    ChangeSet using vault_path_for_source(). Processes all .md files under
    vault_root/queries/.

    Parameters
    ----------
    change_set:
        Raw Phase 1 result from scan_codebase().
    vault_root:
        Absolute path to the vault root directory.
    codebase_root:
        Absolute path to the codebase root (needed for vault_path_for_source()).
    log_fn:
        Callable that appends one formatted string entry to log.md.
    logger:
        Application logger for debug/info/warning/error messages.

    Returns
    -------
    StalenessResult
        Categorized results of the staleness detection pass.
    """
    # Build changed_vault_paths set internally from the raw ChangeSet
    changed_vault_paths: set[str] = set()

    # Deleted summaries are already vault absolute paths → convert to vault-relative strings
    for vault_summary_path in change_set.deleted_summaries:
        try:
            rel = vault_summary_path.relative_to(vault_root)
            changed_vault_paths.add(rel.as_posix())
        except ValueError:
            logger.warning(
                "Could not relativize deleted summary path: %s", vault_summary_path
            )

    # New/modified source files → compute their vault summary paths → convert to vault-relative strings
    for source_file in change_set.new_files + change_set.modified_files:
        try:
            vault_summary_path = vault_path_for_source(source_file, codebase_root, vault_root)
            rel = vault_summary_path.relative_to(vault_root)
            changed_vault_paths.add(rel.as_posix())
        except (ValueError, Exception) as exc:
            logger.warning(
                "Could not compute vault path for source %s: %s", source_file, exc
            )

    result = StalenessResult()
    queries_dir = vault_root / "queries"

    if not queries_dir.is_dir():
        logger.debug("No queries/ directory; staleness detection is a no-op")
        return result

    query_pages = sorted(
        p for p in queries_dir.iterdir() if p.is_file() and p.suffix == ".md"
    )

    if not query_pages:
        logger.debug("No query pages found; staleness detection is a no-op")
        return result

    for query_page in query_pages:
        _process_query_page(
            query_page, changed_vault_paths, vault_root, log_fn, logger, result
        )

    logger.info(
        "Staleness detection complete: flagged=%d already_stale=%d malformed=%d clean=%d",
        len(result.flagged_pages),
        len(result.already_stale_pages),
        len(result.malformed_sources_pages),
        len(result.clean_pages),
    )
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    """Return the current UTC time formatted as 'YYYY-MM-DD HH:MM:SS UTC'."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _parse_sources_section(content: str) -> list[str] | None:
    """Parse ## Sources from query page content.

    Returns:
      None if no ## Sources section found.
      [] if ## Sources section exists but contains no recognizable file paths.
      list[str] of source paths if well-formed.
    """
    m = _SOURCES_HEADING_RE.search(content)
    if not m:
        return None

    # Extract text between ## Sources heading and the next ## heading (or end of file)
    section_start = m.end()
    next_heading = re.search(r"^##\s+", content[section_start:], re.MULTILINE)
    section_end = section_start + next_heading.start() if next_heading else len(content)
    section_text = content[section_start:section_end]

    # Extract all list items: lines starting with "- <path>"
    # _SOURCE_ITEM_RE captures \S+ which stops at whitespace, stripping "(too large...)" annotations
    paths = []
    for item_match in _SOURCE_ITEM_RE.finditer(section_text):
        path_str = item_match.group(1)
        paths.append(path_str.strip())

    return paths  # may be [] if section exists but has no list items


def _normalize_source(source: str) -> str:
    """Normalize a source path from ## Sources to vault-relative forward-slash format."""
    return source.strip().replace("\\", "/")


def _has_stale_banner(content: str) -> bool:
    """Return True if the content already contains a stale callout banner."""
    return bool(_STALE_BANNER_RE.search(content))


def _insert_stale_banner(
    query_page: Path,
    stale_sources: list[str],
    logger: logging.Logger,
) -> None:
    """Insert the stale callout banner immediately after the H1 title in the query page.

    Per FR-3.8 and AT-22: the H1 title must remain the first line of the file.
    The banner is inserted after the H1 and any immediately-following blank lines.
    """
    try:
        content = query_page.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Cannot read %s to insert stale banner: %s", query_page, exc)
        return

    lines = content.splitlines(keepends=True)

    # Find H1 line index
    h1_index = None
    for i, line in enumerate(lines):
        if line.startswith("# "):
            h1_index = i
            break

    if h1_index is None:
        # Fallback: insert at position 1 (after whatever is on line 0)
        insert_index = min(1, len(lines))
        logger.warning(
            "No H1 found in %s; inserting stale banner at position %d",
            query_page.name, insert_index,
        )
    else:
        # Skip blank lines after H1
        insert_index = h1_index + 1
        while insert_index < len(lines) and lines[insert_index].strip() == "":
            insert_index += 1

    # Build banner lines
    sources_str = ", ".join(f"`{s}`" for s in stale_sources)
    banner = [
        "\n",
        "> [!warning] Stale Content\n",
        f"> The following source files changed since this answer was saved: {sources_str}\n",
        "> Run `codewiki lint` to regenerate this answer.\n",
        "\n",
    ]

    new_lines = lines[:insert_index] + banner + lines[insert_index:]
    new_content = "".join(new_lines)

    try:
        query_page.write_text(new_content, encoding="utf-8")
        logger.debug("Inserted stale banner in %s", query_page.name)
    except OSError as exc:
        logger.error("Cannot write stale banner to %s: %s", query_page, exc)


def _annotate_index_row(
    vault_root: Path,
    query_page: Path,
    logger: logging.Logger,
) -> None:
    """Append ' ⚠ stale' to the Description column of the query page's row in index.md."""
    index_path = vault_root / "index.md"
    if not index_path.exists():
        logger.warning(
            "index.md not found; cannot annotate stale row for %s", query_page.name
        )
        return

    try:
        content = index_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Cannot read index.md: %s", exc)
        return

    # Compute the wikilink target for this query page (vault-relative, no .md ext)
    rel = query_page.relative_to(vault_root)
    without_ext = rel.with_suffix("") if rel.suffix == ".md" else rel
    link_target = without_ext.as_posix()   # e.g. "queries/how-does-auth-work"

    new_lines = []
    annotated = False
    for line in content.splitlines(keepends=True):
        if f"[[{link_target}]]" in line and "⚠ stale" not in line:
            # Append stale annotation to Description column
            stripped = line.rstrip("\n")
            # Find last pipe to rebuild row with annotation
            last_pipe = stripped.rfind("|")
            if last_pipe > 0:
                desc_part = stripped[stripped.index("|", stripped.index("[[")) + 1:last_pipe].strip()
                # Rebuild row with annotation
                link_cell = f"[[{link_target}]]"
                new_row = f"| {link_cell} | {desc_part} ⚠ stale |"
                new_lines.append(new_row + "\n")
                annotated = True
                continue
        new_lines.append(line)

    if annotated:
        try:
            index_path.write_text("".join(new_lines), encoding="utf-8")
            logger.debug("Annotated index.md row for %s", query_page.name)
        except OSError as exc:
            logger.error("Cannot write annotated index.md: %s", exc)
    else:
        logger.warning(
            "Could not find index.md row for %s to annotate", query_page.name
        )


def _process_query_page(
    query_page: Path,
    changed_vault_paths: set[str],
    vault_root: Path,
    log_fn: Callable[[str], None],
    logger: logging.Logger,
    result: StalenessResult,
) -> None:
    """Run the full staleness detection pipeline for a single query page."""
    # Step 1 — Read the query page
    try:
        content = query_page.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Cannot read query page %s: %s", query_page, exc)
        result.malformed_sources_pages.append(query_page)
        return

    # Step 2 — Parse ## Sources section
    sources = _parse_sources_section(content)
    if sources is None:
        logger.error(
            "Query page %s has no ## Sources section (hard error)", query_page.name
        )
        ts = _utc_now()
        log_fn(
            f"{ts} | sources-error | "
            f"{query_page.relative_to(vault_root).as_posix()} (missing ## Sources section)"
        )
        result.malformed_sources_pages.append(query_page)
        return
    if not sources:
        logger.error(
            "Query page %s has an empty/malformed ## Sources section (hard error)",
            query_page.name,
        )
        ts = _utc_now()
        log_fn(
            f"{ts} | sources-error | "
            f"{query_page.relative_to(vault_root).as_posix()} (malformed ## Sources section)"
        )
        result.malformed_sources_pages.append(query_page)
        return

    # Step 3 — Check for existing stale banner (AT-23: no duplicate banner)
    if _has_stale_banner(content):
        logger.debug("Query page %s already has stale banner; skipping", query_page.name)
        result.already_stale_pages.append(query_page)
        return

    # Step 4 — Check if any source is in the change-set
    stale_sources = [s for s in sources if _normalize_source(s) in changed_vault_paths]

    if not stale_sources:
        result.clean_pages.append(query_page)
        return

    # Step 5 — Insert stale banner and annotate index
    _insert_stale_banner(query_page, stale_sources, logger)
    _annotate_index_row(vault_root, query_page, logger)

    rel_path = query_page.relative_to(vault_root).as_posix()
    changed_sources_str = ", ".join(stale_sources[:3])  # list first few for log
    ts = _utc_now()
    log_fn(f"{ts} | query-stale | {rel_path} (sources changed: {changed_sources_str})")
    logger.info("Flagged stale: %s", rel_path)
    result.flagged_pages.append(query_page)
