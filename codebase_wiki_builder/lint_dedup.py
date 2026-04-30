"""Semantic deduplication for Codebase Wiki Builder lint command.

Implements deduplicate_query_pages(), which detects and merges near-duplicate
saved query pages using a two-pass LLM strategy:
  1. Cheap detection pass: sends only page titles + descriptions.
  2. Full merge pass: sends complete page content for confirmed duplicate groups.

Public API:
  - LintDedupResult: dataclass summarising what was merged
  - deduplicate_query_pages(): main entry point

No typer imports — this is a pure logic module. The lint CLI (item 17) handles
all framework concerns.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from rich.console import Console

if TYPE_CHECKING:
    from codebase_wiki_builder.llm_client import LLMClient
    from codebase_wiki_builder.query_persistence import QueryPage

logger = logging.getLogger(__name__)
_console = Console()

# ---------------------------------------------------------------------------
# Module-level regex constants
# ---------------------------------------------------------------------------

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_TABLE_ROW_RE = re.compile(r"^\|\s*(\[\[.*?\]\])\s*\|\s*(.*?)\s*\|$")


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------

@dataclass
class LintDedupResult:
    """Result of a deduplicate_query_pages() run."""

    merged_groups: list[tuple[Path, list[Path]]]
    """Each entry is (surviving_page_path, [deleted_page_paths]).
    surviving_page_path is the page that was overwritten with the merged content.
    deleted_page_paths are the pages that were removed from the vault.
    """

    skipped_pages: list[Path]
    """Pages that could not be processed due to I/O or parse errors."""


# ---------------------------------------------------------------------------
# Internal data structure
# ---------------------------------------------------------------------------

@dataclass
class _QueryPageEntry:
    """Lightweight descriptor for one query page row in index.md."""

    wikilink_target: str   # e.g. "queries/how-does-auth-work"
    description: str       # e.g. "Explains how the authentication middleware works"
    path: Path             # absolute path: vault_root / (wikilink_target + ".md")
    row_index: int         # 0-based index in index.md table (for tie-breaking)


# ---------------------------------------------------------------------------
# Step 1 — Collect query page entries from index.md
# ---------------------------------------------------------------------------

def _collect_query_entries(vault_root: Path) -> list[_QueryPageEntry]:
    """Return all query page entries from index.md in row order."""
    index_path = vault_root / "index.md"
    if not index_path.exists():
        return []
    try:
        content = index_path.read_text(encoding="utf-8")
    except OSError:
        return []

    entries: list[_QueryPageEntry] = []
    row_index = 0
    for line in content.splitlines():
        m = _TABLE_ROW_RE.match(line.strip())
        if not m:
            continue
        inner = _WIKILINK_RE.search(m.group(1))
        if not inner:
            continue
        target = inner.group(1)   # e.g. "queries/how-does-auth-work"
        if not target.startswith("queries/"):
            continue
        desc = m.group(2).strip()
        # Strip any stale/unknowable annotations from description for detection pass
        clean_desc = desc.replace(" ⚠ stale", "").replace(" ⊘ unknowable", "").strip()
        entries.append(_QueryPageEntry(
            wikilink_target=target,
            description=clean_desc,
            path=vault_root / (target + ".md"),
            row_index=row_index,
        ))
        row_index += 1

    return entries


# ---------------------------------------------------------------------------
# Step 2 — LLM Detection Pass (titles + descriptions only)
# ---------------------------------------------------------------------------

# DETECTION_PROMPT is kept as documentation only — do NOT use with .format() at runtime.
# Use _build_detection_prompt() which constructs the prompt via an f-string.
DETECTION_PROMPT = """\
You are reviewing a list of saved query pages from a codebase wiki. Each page is identified by its filename and a one-line description of what it covers.

Your task: identify groups of pages that are NEAR-IDENTICAL in intent — meaning a reasonable reader would consider them duplicates answering the same question with trivially different wording. Apply a CONSERVATIVE threshold: err heavily on the side of keeping pages separate. Only flag a group if the pages are nearly identical in topic AND intent. Pages covering the same broad topic from meaningfully different angles should NOT be grouped.

Input — one entry per line in the format "filename: description":
{page_list}

Output a JSON array of duplicate groups. Each group is a JSON array of filenames.
If no duplicates are detected, output an empty array: []

Example output format:
[["queries/how-does-auth-work.md", "queries/explain-authentication.md"]]

Output ONLY the JSON array, no other text.
"""


def _build_detection_prompt(page_list: str) -> str:
    """Build the detection prompt using an f-string.

    Uses an f-string rather than DETECTION_PROMPT.format() so that curly braces
    in untrusted content (index.md descriptions) cannot corrupt the prompt or raise
    KeyError at the Python layer.
    """
    return (
        "You are reviewing a list of saved query pages from a codebase wiki. "
        "Each page is identified by its filename and a one-line description of what it covers.\n"
        "\n"
        "Your task: identify groups of pages that are NEAR-IDENTICAL in intent — meaning a "
        "reasonable reader would consider them duplicates answering the same question with "
        "trivially different wording. Apply a CONSERVATIVE threshold: err heavily on the side "
        "of keeping pages separate. Only flag a group if the pages are nearly identical in "
        "topic AND intent. Pages covering the same broad topic from meaningfully different "
        "angles should NOT be grouped.\n"
        "\n"
        'Input — one entry per line in the format "filename: description":\n'
        f"{page_list}\n"
        "\n"
        "Output a JSON array of duplicate groups. Each group is a JSON array of filenames.\n"
        "If no duplicates are detected, output an empty array: []\n"
        "\n"
        "Example output format:\n"
        '[ ["queries/how-does-auth-work.md", "queries/explain-authentication.md"] ]\n'
        "\n"
        "Output ONLY the JSON array, no other text.\n"
    )


def _run_detection_pass(
    entries: list[_QueryPageEntry],
    llm_client: "LLMClient",
    log: "logging.Logger",
) -> list[list[str]]:
    """Send titles+descriptions to LLM and return list of duplicate filename groups.

    Returns list of groups, each group being a list of filenames (vault-relative with .md).
    Returns [] if no duplicates found or on parse error.
    """
    page_list = "\n".join(
        f"{e.wikilink_target}.md: {e.description}"
        for e in entries
    )
    prompt = _build_detection_prompt(page_list)

    try:
        response = llm_client.complete(prompt)
    except Exception as exc:
        log.error("LLM detection pass failed: %s", exc)
        return []

    # Parse JSON array from response
    response_stripped = response.strip()
    # Find the first '[' and last ']' to extract just the JSON
    start = response_stripped.find("[")
    end = response_stripped.rfind("]")
    if start == -1 or end == -1:
        log.warning("Detection pass response has no JSON array; assuming no duplicates")
        return []

    try:
        groups = json.loads(response_stripped[start:end + 1])
    except json.JSONDecodeError as exc:
        log.warning("Cannot parse detection pass JSON: %s — assuming no duplicates", exc)
        return []

    if not isinstance(groups, list):
        log.warning("Detection pass returned non-list; assuming no duplicates")
        return []

    # Validate: each group must be a list of strings, minimum 2 entries
    valid_groups: list[list[str]] = []
    for group in groups:
        if isinstance(group, list) and len(group) >= 2 and all(isinstance(f, str) for f in group):
            valid_groups.append(group)
        else:
            log.warning("Ignoring malformed duplicate group: %r", group)

    return valid_groups


# ---------------------------------------------------------------------------
# Step 3 — Determine Recency
# ---------------------------------------------------------------------------

def _parse_timestamp(ts_str: str) -> datetime | None:
    """Parse 'YYYY-MM-DD HH:MM:SS UTC' into a datetime. Returns None on failure."""
    if not ts_str:
        return None
    try:
        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S UTC").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _most_recent_page(
    pages: list["QueryPage"],
    entry_map: dict[str, "_QueryPageEntry"],
    vault_root: Path,
) -> "QueryPage":
    """Return the most recently updated page from a group.

    Recency order:
      1. updated_at timestamp (parsed from ## Page Metadata)
      2. saved_at timestamp (fallback if updated_at absent/invalid)
      3. row_index from index.md (higher index = more recent; last resort)

    The entry_map is keyed by wikilink_target (e.g. "queries/how-does-auth-work"),
    derived as page.path.relative_to(vault_root).with_suffix("").as_posix().
    """
    def recency_key(page: "QueryPage") -> tuple:
        # Derive wikilink_target key from path: e.g. "queries/how-does-auth-work"
        wikilink_target = page.path.relative_to(vault_root).with_suffix("").as_posix()
        # Find matching entry for row_index
        entry = entry_map.get(wikilink_target)
        row_idx = entry.row_index if entry else 0

        updated = _parse_timestamp(page.updated_at)
        saved = _parse_timestamp(page.saved_at)

        # Use updated_at if valid, else saved_at, else epoch
        best_ts = updated or saved or datetime.min.replace(tzinfo=timezone.utc)
        return (best_ts, row_idx)

    return max(pages, key=recency_key)


# ---------------------------------------------------------------------------
# Step 4 — LLM Merge Pass
# ---------------------------------------------------------------------------

# MERGE_PROMPT is kept as documentation only — do NOT use with .format() at runtime.
# Use _build_merge_prompt() which constructs the prompt via an f-string.
MERGE_PROMPT = """\
You are merging multiple near-duplicate saved query pages from a codebase wiki into one comprehensive, well-written answer. The pages below answer the same question with slightly different wording.

Produce a single merged answer that:
- Uses the question from the most recently updated page as the H1 title (it is marked as SURVIVING PAGE below)
- Synthesises the best content from all pages
- Avoids redundancy
- Ends with a combined ## Sources section listing all unique source files from all pages

Output ONLY the merged page content starting with the H1 title line. Do not include ## Page Metadata — that will be added separately.

--- PAGES TO MERGE ---
{pages_content}
--- END PAGES ---
"""


def _build_merge_prompt(pages_content: str) -> str:
    """Build the merge prompt using an f-string.

    Uses an f-string rather than MERGE_PROMPT.format() so that curly braces in
    untrusted content (query page body text) cannot corrupt the prompt or raise
    KeyError at the Python layer.
    """
    return (
        "You are merging multiple near-duplicate saved query pages from a codebase wiki "
        "into one comprehensive, well-written answer. The pages below answer the same "
        "question with slightly different wording.\n"
        "\n"
        "Produce a single merged answer that:\n"
        "- Uses the question from the most recently updated page as the H1 title "
        "(it is marked as SURVIVING PAGE below)\n"
        "- Synthesises the best content from all pages\n"
        "- Avoids redundancy\n"
        "- Ends with a combined ## Sources section listing all unique source files from all pages\n"
        "\n"
        "Output ONLY the merged page content starting with the H1 title line. "
        "Do not include ## Page Metadata — that will be added separately.\n"
        "\n"
        "--- PAGES TO MERGE ---\n"
        f"{pages_content}\n"
        "--- END PAGES ---\n"
    )


def _run_merge_pass(
    pages: list["QueryPage"],
    surviving_page: "QueryPage",
    llm_client: "LLMClient",
    log: "logging.Logger",
) -> str:
    """Send all pages' content to LLM and return merged page content (H1 + answer + ## Sources).

    Returns the raw LLM response (caller adds ## Page Metadata).
    On LLM error, returns the surviving page's raw content without ## Page Metadata as fallback.
    """
    sections: list[str] = []
    for page in pages:
        label = "SURVIVING PAGE" if page.path == surviving_page.path else "DUPLICATE PAGE"
        sections.append(
            f"--- {label}: {page.path.name} ---\n{page.raw_content}"
        )

    pages_content = "\n\n".join(sections)
    prompt = _build_merge_prompt(pages_content)

    try:
        return llm_client.complete(prompt)
    except Exception as exc:
        log.error("LLM merge pass failed for group %s: %s", surviving_page.path.name, exc)
        # Fallback: return surviving page content without ## Page Metadata
        raw = surviving_page.raw_content
        metadata_match = re.search(r"^##\s+Page Metadata\s*$", raw, re.MULTILINE)
        if metadata_match:
            return raw[:metadata_match.start()].rstrip()
        return raw


# ---------------------------------------------------------------------------
# Step 5 — Write merged page
# ---------------------------------------------------------------------------

def _write_merged_page(
    surviving_page: "QueryPage",
    merged_body: str,
    timestamp: str,
) -> None:
    """Write the merged content to the surviving page's path.

    Preserves saved_at from the surviving page; sets updated_at to current timestamp.
    The merged_body already contains the H1 title, answer, and ## Sources section.
    """
    parts = [
        merged_body.rstrip(),
        "",
        "## Page Metadata",
        f"saved_at: {surviving_page.saved_at}",
        f"updated_at: {timestamp}",
        "",
    ]
    content = "\n".join(parts)
    surviving_page.path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Step 6 — Update index.md after merge
# ---------------------------------------------------------------------------

def _update_index_for_merge(
    vault_root: Path,
    surviving_entry: "_QueryPageEntry",
    deleted_entries: list["_QueryPageEntry"],
    new_description: str,
    log: "logging.Logger",
) -> None:
    """Replace all group rows in index.md with one row for the merged page.

    Keeps all other rows intact. The merged row uses surviving_entry's wikilink.
    """
    index_path = vault_root / "index.md"
    try:
        content = index_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.error("Cannot read index.md for merge update: %s", exc)
        return

    surviving_link = f"[[{surviving_entry.wikilink_target}]]"
    safe_desc = new_description.replace("|", "\\|")
    merged_row = f"| {surviving_link} | {safe_desc} |"

    deleted_targets = {e.wikilink_target for e in deleted_entries}
    surviving_target = surviving_entry.wikilink_target

    new_lines: list[str] = []
    surviving_row_written = False

    for line in content.splitlines():
        # Check if this line is a row for any page in the group
        inner = _WIKILINK_RE.search(line)
        if not inner:
            new_lines.append(line)
            continue

        target = inner.group(1)
        if target == surviving_target:
            # Replace with merged row (write only once)
            if not surviving_row_written:
                new_lines.append(merged_row)
                surviving_row_written = True
            # else: skip duplicate (shouldn't happen, but defensive)
        elif target in deleted_targets:
            # Drop this row entirely
            pass
        else:
            new_lines.append(line)

    try:
        index_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        log.debug(
            "Updated index.md for merge: kept %s, removed %d rows",
            surviving_target, len(deleted_targets),
        )
    except OSError as exc:
        log.error("Cannot write updated index.md after merge: %s", exc)


# ---------------------------------------------------------------------------
# Step 7 — Logging and terminal output
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    """Return the current UTC time as a formatted string."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _log_deduplicated(
    deleted_path: Path,
    surviving_path: Path,
    vault_root: Path,
    log_fn: Callable[[str], None],
) -> None:
    """Write a lint-deduplicated log entry for one deleted page."""
    ts = _utc_now()
    deleted_rel = deleted_path.relative_to(vault_root).as_posix()
    surviving_rel = surviving_path.relative_to(vault_root).as_posix()
    log_fn(f"{ts} | lint-deduplicated | {deleted_rel} → {surviving_rel}")


# ---------------------------------------------------------------------------
# Helper — extract first prose line for index description
# ---------------------------------------------------------------------------

def _extract_first_prose(content: str) -> str:
    """Extract first prose line from merged content for index.md description."""
    skipped_h1 = False
    for line in content.splitlines():
        stripped = line.strip()
        if not skipped_h1 and stripped.startswith("# "):
            skipped_h1 = True
            continue
        if not stripped:
            continue
        if stripped.startswith("#"):
            break
        return stripped[:120]
    return "(merged query page)"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def deduplicate_query_pages(
    vault_root: Path,
    llm_client: "LLMClient",
    log_fn: Callable[[str], None],
) -> LintDedupResult:
    """Detect and merge semantically duplicate saved query pages.

    Steps:
      1. Collect all query pages from index.md.
      2. If fewer than 2 pages: skip; print "No duplicate query pages found."; return empty result.
      3. Send titles+descriptions to LLM for detection pass.
      4. Parse LLM response as JSON array of duplicate groups.
      5. For each duplicate group:
         a. Read full content of all pages in the group.
         b. Determine most-recently-updated page (recency by updated_at → saved_at → row order).
         c. Send all pages' content to LLM for merge.
         d. Write merged page overwriting the most-recent-page slug.
         e. Delete all other pages in the group from the vault.
         f. Update index.md: replace all group rows with one row for the merged page.
         g. Log one lint-deduplicated entry per deleted page.
         h. Print terminal output.
      6. Print final summary.
      7. Return LintDedupResult.
    """
    from codebase_wiki_builder.query_persistence import read_query_page

    log = logging.getLogger(__name__)

    entries = _collect_query_entries(vault_root)

    if len(entries) < 2:
        _console.print("No duplicate query pages found.")
        return LintDedupResult(merged_groups=[], skipped_pages=[])

    # Detection pass: cheap LLM call using only titles + descriptions
    duplicate_groups_filenames = _run_detection_pass(entries, llm_client, log)

    if not duplicate_groups_filenames:
        _console.print("No duplicate query pages found.")
        return LintDedupResult(merged_groups=[], skipped_pages=[])

    # Build lookup maps
    # keyed by "queries/how-does-auth-work.md" (wikilink_target + ".md")
    filename_to_entry: dict[str, _QueryPageEntry] = {
        e.wikilink_target + ".md": e for e in entries
    }
    # keyed by "queries/how-does-auth-work" (wikilink_target, no .md suffix)
    target_to_entry: dict[str, _QueryPageEntry] = {
        e.wikilink_target: e for e in entries
    }

    merged_groups: list[tuple[Path, list[Path]]] = []
    skipped: list[Path] = []

    for group_filenames in duplicate_groups_filenames:
        # Resolve filenames to QueryPage objects
        pages: list[QueryPage] = []
        group_skipped: list[Path] = []

        for filename in group_filenames:
            entry = filename_to_entry.get(filename)
            if not entry:
                log.warning("Duplicate group contains unknown filename: %s", filename)
                continue
            if not entry.path.exists():
                log.warning("Duplicate group page not found on disk: %s", entry.path)
                group_skipped.append(entry.path)
                continue
            try:
                page = read_query_page(entry.path)
                pages.append(page)
            except (OSError, ValueError) as exc:
                log.error("Cannot read page %s for dedup: %s", entry.path, exc)
                group_skipped.append(entry.path)

        skipped.extend(group_skipped)

        if len(pages) < 2:
            log.warning("Dedup group has fewer than 2 readable pages after loading; skipping")
            continue

        # Determine the surviving page (most recently updated)
        surviving_page = _most_recent_page(pages, target_to_entry, vault_root)
        deleted_pages = [p for p in pages if p.path != surviving_page.path]

        # Derive surviving_entry key: wikilink_target (no .md suffix) from path
        surviving_target_key = surviving_page.path.relative_to(vault_root).with_suffix("").as_posix()
        surviving_entry = target_to_entry.get(surviving_target_key)
        if surviving_entry is None:
            log.error(
                "Cannot find index entry for surviving page %s; skipping group",
                surviving_page.path,
            )
            skipped.extend(p.path for p in pages)
            continue

        deleted_entries: list[_QueryPageEntry] = []
        for p in deleted_pages:
            deleted_target_key = p.path.relative_to(vault_root).with_suffix("").as_posix()
            entry = target_to_entry.get(deleted_target_key)
            if entry is not None:
                deleted_entries.append(entry)

        # Merge pass: full content LLM call
        merged_body = _run_merge_pass(pages, surviving_page, llm_client, log)

        timestamp = _utc_now()

        # Write merged page (overwrite surviving page)
        try:
            _write_merged_page(surviving_page, merged_body, timestamp)
        except OSError as exc:
            log.error("Cannot write merged page %s: %s", surviving_page.path, exc)
            skipped.extend(p.path for p in pages)
            continue

        # Delete other pages
        for deleted_page in deleted_pages:
            try:
                deleted_page.path.unlink()
                log.info("Deleted duplicate: %s", deleted_page.path)
            except OSError as exc:
                log.error("Cannot delete duplicate %s: %s", deleted_page.path, exc)
                skipped.append(deleted_page.path)

        # Extract description from merged body for index update
        new_description = _extract_first_prose(merged_body)

        # Update index.md
        _update_index_for_merge(
            vault_root, surviving_entry, deleted_entries, new_description, log
        )

        # Log and print per deleted page
        for deleted_page in deleted_pages:
            _log_deduplicated(deleted_page.path, surviving_page.path, vault_root, log_fn)
            deleted_rel = deleted_page.path.relative_to(vault_root).as_posix()
            surviving_rel = surviving_page.path.relative_to(vault_root).as_posix()
            _console.print(f"[green]✓ Merged:[/green] {deleted_rel} → {surviving_rel}")

        merged_groups.append((surviving_page.path, [p.path for p in deleted_pages]))

    # Final summary
    total_merged = sum(len(deleted) for _, deleted in merged_groups)
    surviving_count = len(merged_groups)
    if merged_groups:
        _console.print(
            f"Deduplication complete: {total_merged + surviving_count} pages merged into "
            f"{surviving_count} page(s)."
        )
    else:
        _console.print("No duplicate query pages found.")

    return LintDedupResult(merged_groups=merged_groups, skipped_pages=skipped)
