"""Query page persistence for Codebase Wiki Builder.

Provides save_query_page() and read_query_page() for persisting and parsing
saved query answer pages in the vault's queries/ directory.

Public API:
  - QueryPage: dataclass representing a parsed saved query page (for lint use)
  - save_query_page(): persist a query result to queries/<slug>.md
  - read_query_page(): parse an existing saved query page into a QueryPage

No typer or rich imports — this is a pure persistence/utility module usable
by both the CLI and MCP server.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from codebase_wiki_builder.query_engine import QueryResult

from codebase_wiki_builder.vault import slugify, wikilink

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns for parsing
# ---------------------------------------------------------------------------

_H1_RE = re.compile(r"^# (.+)$", re.MULTILINE)
_SOURCES_HEADING_RE = re.compile(r"^##\s+Sources\s*$", re.MULTILINE)
_SOURCE_ITEM_RE = re.compile(r"^\s*-\s+(\S+)", re.MULTILINE)
_PAGE_METADATA_RE = re.compile(r"^##\s+Page Metadata\s*$", re.MULTILINE)
_SAVED_AT_RE = re.compile(r"^saved_at:\s*(.+)$", re.MULTILINE)
_UPDATED_AT_RE = re.compile(r"^updated_at:\s*(.+)$", re.MULTILINE)


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------

@dataclass
class QueryPage:
    """A parsed saved query page, used by the lint command (item 14)."""

    path: Path
    """Absolute path to the query page file."""

    question: str
    """The H1 title / original question."""

    answer_body: str
    """The full answer text between the H1 (and any banner) and ## Sources heading."""

    sources: list[str]
    """Vault-relative paths from the ## Sources section."""

    saved_at: str
    """Timestamp string from ## Page Metadata saved_at field (e.g. '2026-04-29 10:00:00 UTC')."""

    updated_at: str
    """Timestamp string from ## Page Metadata updated_at field."""

    raw_content: str
    """The complete raw file content, used when lint rewrites the page."""


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def save_query_page(
    question: str,
    result: "QueryResult",
    vault_root: Path,
    log_fn: Callable[[str], None],
) -> Path:
    """Persist a query result to queries/<slug>.md.

    Steps:
      1. Generate slug from question.
      2. Deduplicate: find an unused filename in queries/ using numeric suffix.
      3. Create queries/ directory if it does not exist.
      4. Build page content (H1, answer body, ## Sources, ## Page Metadata).
      5. Write the file (never overwrites an existing file).
      6. Append a row to index.md using result.one_line_summary.
      7. Append a query-saved entry to log.md.
      8. Return the saved file path.

    Args:
        question: The user's original question (becomes H1 title).
        result: QueryResult with answer (including ## Sources), sources, and one_line_summary.
        vault_root: Absolute path to the vault root directory.
        log_fn: Callable that accepts a pre-formatted log entry string and writes it to log.md.

    Returns:
        The absolute Path of the saved query page file.

    Raises:
        OSError: if the queries/ directory cannot be created or the file cannot be written.
    """
    # 1. Slug + deduplication
    slug = _make_slug(question)
    queries_dir = vault_root / "queries"
    queries_dir.mkdir(parents=True, exist_ok=True)
    page_path = _unique_query_path(queries_dir, slug)

    # 2. Build content and write (never overwrites — _unique_query_path guarantees unused path)
    timestamp = _utc_now()
    content = _build_page_content(question, result, timestamp)
    page_path.write_text(content, encoding="utf-8")
    logger.info("Saved query page: %s", page_path)

    # 3. Append index.md row
    _append_index_row(vault_root, page_path, result.one_line_summary, logger)

    # 4. Log query-saved entry
    _write_log_entry(question, page_path, vault_root, timestamp, log_fn)

    return page_path


def read_query_page(path: Path) -> QueryPage:
    """Parse a saved query page file into a QueryPage dataclass.

    Args:
        path: Absolute path to the query page .md file.

    Returns:
        A QueryPage with all fields populated.

    Raises:
        OSError: if the file cannot be read.
        ValueError: if the file is missing the required H1 title line.
    """
    raw_content = path.read_text(encoding="utf-8")

    # Extract H1 question
    h1_match = _H1_RE.search(raw_content)
    if not h1_match:
        raise ValueError(f"Query page {path} has no H1 title line")
    question = h1_match.group(1).strip()

    # Extract sources list
    sources = _extract_sources(raw_content)

    # Extract timestamps from ## Page Metadata
    saved_at = _extract_field(raw_content, _SAVED_AT_RE, default="")
    updated_at = _extract_field(raw_content, _UPDATED_AT_RE, default="")

    # Extract answer body (between H1 end and ## Sources heading)
    answer_body = _extract_answer_body(raw_content)

    return QueryPage(
        path=path,
        question=question,
        answer_body=answer_body,
        sources=sources,
        saved_at=saved_at,
        updated_at=updated_at,
        raw_content=raw_content,
    )


# ---------------------------------------------------------------------------
# Internal helpers — slug generation
# ---------------------------------------------------------------------------

def _make_slug(question: str) -> str:
    """Convert question to URL-safe slug. Falls back to 'query' if result is empty."""
    slug = slugify(question)
    return slug if slug else "query"


def _unique_query_path(queries_dir: Path, slug: str) -> Path:
    """Return the path for a new, non-conflicting query page file.

    queries/slug.md         — if unused
    queries/slug-2.md       — if slug.md exists
    queries/slug-3.md       — if slug-2.md also exists
    ... and so on.
    """
    candidate = queries_dir / f"{slug}.md"
    if not candidate.exists():
        return candidate

    suffix = 2
    while True:
        candidate = queries_dir / f"{slug}-{suffix}.md"
        if not candidate.exists():
            return candidate
        suffix += 1


# ---------------------------------------------------------------------------
# Internal helpers — page content
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    """Return the current UTC time as a formatted string."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _build_page_content(question: str, result: "QueryResult", timestamp: str) -> str:
    """Assemble the complete query page content.

    Args:
        question: The original question (becomes the H1 title).
        result: QueryResult containing answer (with embedded ## Sources) and sources list.
        timestamp: UTC timestamp string used for both saved_at and updated_at at creation.

    Returns:
        Complete page content as a string, ready to write to disk.
    """
    parts = [
        f"# {question}",
        "",
        result.answer,    # includes answer body + ## Sources section
        "",
        "## Page Metadata",
        f"saved_at: {timestamp}",
        f"updated_at: {timestamp}",
        "",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Internal helpers — index.md append
# ---------------------------------------------------------------------------

def _append_index_row(
    vault_root: Path,
    page_path: Path,
    description: str,
    log: logging.Logger,
) -> None:
    """Append one row to index.md for the newly saved query page.

    If index.md does not exist, creates it with the table header first.
    The description is taken from result.one_line_summary (LLM-generated).
    """
    index_path = vault_root / "index.md"
    link = wikilink(page_path, vault_root)
    safe_desc = description.replace("|", "\\|")
    new_row = f"| {link} | {safe_desc} |\n"

    if not index_path.exists():
        # Create index.md with header (edge case: query before first ingest completes)
        header = "| File | Description |\n|------|-------------|\n"
        index_path.write_text(header + new_row, encoding="utf-8")
        log.debug("Created index.md with query page row: %s", link)
        return

    try:
        existing = index_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.error("Cannot read index.md to append query row: %s", exc)
        return

    # Append the new row at the end of the file
    updated = existing.rstrip("\n") + "\n" + new_row
    try:
        index_path.write_text(updated, encoding="utf-8")
        log.debug("Appended index.md row for %s", link)
    except OSError as exc:
        log.error("Cannot write updated index.md: %s", exc)


# ---------------------------------------------------------------------------
# Internal helpers — log entry
# ---------------------------------------------------------------------------

def _write_log_entry(
    question: str,
    page_path: Path,
    vault_root: Path,
    timestamp: str,
    log_fn: Callable[[str], None],
) -> None:
    """Write a query-saved entry to log.md via the provided log_fn callable."""
    rel_path = page_path.relative_to(vault_root).as_posix()
    log_fn(f"{timestamp} | query-saved | {question} → {rel_path}")


# ---------------------------------------------------------------------------
# Internal helpers — parsing
# ---------------------------------------------------------------------------

def _extract_sources(content: str) -> list[str]:
    """Extract source paths from ## Sources section. Returns [] if absent."""
    m = _SOURCES_HEADING_RE.search(content)
    if not m:
        return []
    section_start = m.end()
    next_heading = re.search(r"^##\s+", content[section_start:], re.MULTILINE)
    section_end = section_start + next_heading.start() if next_heading else len(content)
    section_text = content[section_start:section_end]
    return [item.group(1).strip() for item in _SOURCE_ITEM_RE.finditer(section_text)]


def _extract_field(content: str, pattern: re.Pattern, default: str) -> str:
    """Extract a single-line field value using the given compiled regex pattern."""
    m = pattern.search(content)
    return m.group(1).strip() if m else default


def _extract_answer_body(content: str) -> str:
    """Extract the answer body text.

    Starts after the H1 line (and any callout banner blocks that follow it).
    Ends before the first ## section heading.
    Returns stripped answer text.
    """
    lines = content.splitlines()
    # Find H1 line index
    h1_idx = next((i for i, line in enumerate(lines) if line.startswith("# ")), None)
    if h1_idx is None:
        return ""

    # Find first ## section heading after H1
    section_idx = next(
        (i for i in range(h1_idx + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )

    # Slice body lines
    body_lines = lines[h1_idx + 1 : section_idx]
    # Strip leading blank lines and callout banner blocks (> lines)
    start = 0
    while start < len(body_lines):
        stripped = body_lines[start].strip()
        if stripped == "" or stripped.startswith(">"):
            start += 1
        else:
            break

    return "\n".join(body_lines[start:]).strip()
