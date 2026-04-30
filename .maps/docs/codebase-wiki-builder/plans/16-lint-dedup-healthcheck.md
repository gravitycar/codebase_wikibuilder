# Implementation Plan: Lint Part 2 — Semantic Deduplication and Part 3 — Deep Health-Check

## Spec Context

This plan implements the final two parts of the `lint` command. Part 2 detects near-duplicate query pages using a cheap LLM detection pass over titles and descriptions only, then merges confirmed duplicate groups by sending full page content to the LLM. Part 3 runs a deep health-check of the entire vault by batching actual summary file content through the same tiktoken directory-subdivision algorithm used by the `analysis` command, then synthesizes findings into `lint-report.md`.

Catalog item: 16 — Lint Part 2: Semantic Deduplication and Part 3: Deep Health-Check
Specification section: FR-8.2 (deduplication), FR-8.3 (health-check), FR-6.1 (`lint-deduplicated` log entry)
Acceptance criteria addressed: AT-16 (semantic deduplication: merged content, metadata preservation, index update, log entry, terminal output), AT-17 (deep health-check: `lint-report.md` with four section headers)

## Dependencies

- **Blocked by**:
  - Item 4 (Vault File Utilities + Logging) — needs `wikilink()`, `append_log_md()`, `slugify()`, vault walk helpers, `EXCLUDED_DIRS`
  - Item 8 (Index + Staleness) — needs `_parse_existing_index()` pattern; index must be up-to-date when dedup runs
  - Item 10 (Analysis) — needs `ANALYSIS_CONTEXT_WINDOW` constant and `build_batches()` / `collect_summary_files()` helpers; health-check uses the identical batching strategy
  - Item 14 (Lint Part 1) — dedup and health-check always run after Part 1 completes; `LintStalenessResult` is available if needed
- **Blocks**: Item 17 (Lint CLI + Help) — the lint CLI orchestrates Parts 1, 2, 3 in sequence; this item must be built before item 17 wires the `lint` subcommand
- **Uses**: `pathlib`, `re`, `datetime`, `dataclasses`, `logging` (all stdlib); `tiktoken` (token counting); `LLMClient` from `llm_client.py`; `wikilink()`, `slugify()` from `vault.py`; `append_log_md()` from `logging_setup.py`; `ANALYSIS_CONTEXT_WINDOW`, `build_batches()`, `collect_summary_files()` from `analysis.py`; `read_query_page()`, `QueryPage` from `query_persistence.py`

## File Changes

### New Files

- `codebase_wiki_builder/lint_dedup.py` — `LintDedupResult` dataclass, `deduplicate_query_pages()`, all detection/merge/file-operation helpers
- `codebase_wiki_builder/lint_healthcheck.py` — `run_health_check()`, batch analysis helpers, synthesis, `lint-report.md` writer

### Modified Files

- None (both are new modules; lint CLI wiring is item 17)

---

## Implementation Details

---

### Module A: `lint_dedup.py`

**File**: `codebase_wiki_builder/lint_dedup.py`

**Exports**:
- `LintDedupResult` — dataclass summarising what was merged
- `deduplicate_query_pages(vault_root: Path, llm_client: LLMClient, log_fn: Callable[[str], None]) -> LintDedupResult`

---

#### `LintDedupResult` Dataclass

```python
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LintDedupResult:
    merged_groups: list[tuple[Path, list[Path]]]
    """Each entry is (surviving_page_path, [deleted_page_paths]).
    surviving_page_path is the page that was overwritten with the merged content.
    deleted_page_paths are the pages that were removed from the vault.
    """

    skipped_pages: list[Path]
    """Pages that could not be processed due to I/O or parse errors."""
```

---

#### `deduplicate_query_pages()` — Main Entry Point

**Signature**:

```python
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
```

---

#### Step 1 — Collect Query Pages from `index.md`

Parse `index.md` for all rows whose wikilink target begins with `queries/`. Preserve row order (used as tie-breaker for recency).

```python
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_TABLE_ROW_RE = re.compile(r"^\|\s*(\[\[.*?\]\])\s*\|\s*(.*?)\s*\|$")


@dataclass
class _QueryPageEntry:
    """Lightweight descriptor for one query page row in index.md."""
    wikilink_target: str   # e.g. "queries/how-does-auth-work"
    description: str        # e.g. "Explains how the authentication middleware works"
    path: Path              # absolute path: vault_root / (wikilink_target + ".md")
    row_index: int          # 0-based index in index.md table (for tie-breaking)


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
```

---

#### Step 2 — LLM Detection Pass (titles + descriptions only)

Send the list of page filenames and their one-line descriptions to the LLM. Instruct it to identify groups of pages that are near-identical in intent. This is the cheap pass — full content is NOT read yet.

```python
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
    logger: "logging.Logger",
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
        logger.error("LLM detection pass failed: %s", exc)
        return []

    # Parse JSON array from response
    response_stripped = response.strip()
    # Find the first '[' and last ']' to extract just the JSON
    start = response_stripped.find("[")
    end = response_stripped.rfind("]")
    if start == -1 or end == -1:
        logger.warning("Detection pass response has no JSON array; assuming no duplicates")
        return []

    import json
    try:
        groups = json.loads(response_stripped[start:end + 1])
    except json.JSONDecodeError as exc:
        logger.warning("Cannot parse detection pass JSON: %s — assuming no duplicates", exc)
        return []

    if not isinstance(groups, list):
        logger.warning("Detection pass returned non-list; assuming no duplicates")
        return []

    # Validate: each group must be a list of strings, minimum 2 entries
    valid_groups: list[list[str]] = []
    for group in groups:
        if isinstance(group, list) and len(group) >= 2 and all(isinstance(f, str) for f in group):
            valid_groups.append(group)
        else:
            logger.warning("Ignoring malformed duplicate group: %r", group)

    return valid_groups
```

---

#### Step 3 — Determine Recency

For a duplicate group, identify the most recently updated page using the recency rules from FR-8.2:
1. Compare `updated_at` timestamps from `## Page Metadata`.
2. If `updated_at` is absent or unparseable, fall back to `saved_at`.
3. If both are absent, treat row order in `index.md` as tiebreaker (later row = more recent).

```python
from datetime import datetime, timezone


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
```

The `entry_map` is a dict mapping wikilink_target (e.g., `"queries/how-does-auth-work"`) to its `_QueryPageEntry`. The key is derived from each page's path using `page.path.relative_to(vault_root).with_suffix("").as_posix()`, which produces the same key format as `_QueryPageEntry.wikilink_target`. This gives access to `row_index` for the tiebreaker.

---

#### Step 4 — LLM Merge Pass

For a confirmed duplicate group, send the full content of all pages to the LLM and ask it to produce a single merged answer.

```python
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
    logger: "logging.Logger",
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
        logger.error("LLM merge pass failed for group %s: %s", surviving_page.path.name, exc)
        # Fallback: return surviving page content without ## Page Metadata
        raw = surviving_page.raw_content
        metadata_match = re.search(r"^##\s+Page Metadata\s*$", raw, re.MULTILINE)
        if metadata_match:
            return raw[:metadata_match.start()].rstrip()
        return raw
```

---

#### Step 5 — Write Merged Page and Delete Others

```python
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
```

Key: `saved_at` comes from the original surviving page (preserved); `updated_at` is the current run timestamp (per FR-8.2(d)).

---

#### Step 6 — Update `index.md`

Replace all rows for the duplicate group with a single row for the merged (surviving) page. The description comes from the LLM-generated first line of the merged body (or the surviving page's original description as fallback).

```python
def _update_index_for_merge(
    vault_root: Path,
    surviving_entry: "_QueryPageEntry",
    deleted_entries: list["_QueryPageEntry"],
    new_description: str,
    logger: "logging.Logger",
) -> None:
    """Replace all group rows in index.md with one row for the merged page.

    Keeps all other rows intact. The merged row uses surviving_entry's wikilink.
    """
    index_path = vault_root / "index.md"
    try:
        content = index_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Cannot read index.md for merge update: %s", exc)
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
        logger.debug("Updated index.md for merge: kept %s, removed %d rows",
                     surviving_target, len(deleted_targets))
    except OSError as exc:
        logger.error("Cannot write updated index.md after merge: %s", exc)
```

---

#### Step 7 — Log and Terminal Output

Per FR-8.2(g-h): one `lint-deduplicated` log entry per deleted page; terminal output per merge.

```python
def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _log_deduplicated(
    deleted_path: Path,
    surviving_path: Path,
    vault_root: Path,
    log_fn: "Callable[[str], None]",
) -> None:
    ts = _utc_now()
    deleted_rel = deleted_path.relative_to(vault_root).as_posix()
    surviving_rel = surviving_path.relative_to(vault_root).as_posix()
    log_fn(f"{ts} | lint-deduplicated | {deleted_rel} → {surviving_rel}")
```

Terminal output (using `rich.console.Console`):

```python
# Print: ✓ Merged: queries/explain-authentication.md → queries/how-does-auth-work.md
_console.print(
    f"[green]✓ Merged:[/green] {deleted_rel} → {surviving_rel}"
)
```

---

#### Complete `deduplicate_query_pages()` Body

```python
def deduplicate_query_pages(
    vault_root: Path,
    llm_client: "LLMClient",
    log_fn: Callable[[str], None],
) -> LintDedupResult:
    from codebase_wiki_builder.query_persistence import read_query_page

    logger = logging.getLogger(__name__)

    entries = _collect_query_entries(vault_root)

    if len(entries) < 2:
        _console.print("No duplicate query pages found.")
        return LintDedupResult(merged_groups=[], skipped_pages=[])

    # Detection pass: cheap LLM call using only titles + descriptions
    duplicate_groups_filenames = _run_detection_pass(entries, llm_client, logger)

    if not duplicate_groups_filenames:
        _console.print("No duplicate query pages found.")
        return LintDedupResult(merged_groups=[], skipped_pages=[])

    # Build lookup maps
    filename_to_entry: dict[str, _QueryPageEntry] = {
        e.wikilink_target + ".md": e for e in entries
    }
    # Map wikilink_target (no .md) → entry for recency lookup
    target_to_entry: dict[str, _QueryPageEntry] = {
        e.wikilink_target: e for e in entries
    }

    merged_groups: list[tuple[Path, list[Path]]] = []
    skipped: list[Path] = []

    for group_filenames in duplicate_groups_filenames:
        # Resolve filenames to QueryPage objects
        pages: list["QueryPage"] = []
        group_skipped: list[Path] = []

        for filename in group_filenames:
            entry = filename_to_entry.get(filename)
            if not entry:
                logger.warning("Duplicate group contains unknown filename: %s", filename)
                continue
            if not entry.path.exists():
                logger.warning("Duplicate group page not found on disk: %s", entry.path)
                group_skipped.append(entry.path)
                continue
            try:
                page = read_query_page(entry.path)
                pages.append(page)
            except (OSError, ValueError) as exc:
                logger.error("Cannot read page %s for dedup: %s", entry.path, exc)
                group_skipped.append(entry.path)

        skipped.extend(group_skipped)

        if len(pages) < 2:
            logger.warning("Dedup group has fewer than 2 readable pages after loading; skipping")
            continue

        # Determine the surviving page (most recently updated)
        surviving_page = _most_recent_page(pages, target_to_entry, vault_root)
        deleted_pages = [p for p in pages if p.path != surviving_page.path]

        surviving_entry = filename_to_entry[surviving_page.path.name]
        deleted_entries = [filename_to_entry[p.path.name] for p in deleted_pages
                           if p.path.name in filename_to_entry]

        # Merge pass: full content LLM call
        merged_body = _run_merge_pass(pages, surviving_page, llm_client, logger)

        timestamp = _utc_now()

        # Write merged page (overwrite surviving page)
        try:
            _write_merged_page(surviving_page, merged_body, timestamp)
        except OSError as exc:
            logger.error("Cannot write merged page %s: %s", surviving_page.path, exc)
            skipped.extend(p.path for p in pages)
            continue

        # Delete other pages
        for deleted_page in deleted_pages:
            try:
                deleted_page.path.unlink()
                logger.info("Deleted duplicate: %s", deleted_page.path)
            except OSError as exc:
                logger.error("Cannot delete duplicate %s: %s", deleted_page.path, exc)
                skipped.append(deleted_page.path)

        # Extract description from merged body for index update
        new_description = _extract_first_prose(merged_body)

        # Update index.md
        _update_index_for_merge(
            vault_root, surviving_entry, deleted_entries, new_description, logger
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
```

---

#### `_extract_first_prose()` Helper

Extracts a one-line description from the merged body for the `index.md` entry. Same logic as `_extract_description()` in `index_writer.py`: skip the H1, skip blanks, return first prose line (up to 120 chars).

```python
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
```

---

### Module B: `lint_healthcheck.py`

**File**: `codebase_wiki_builder/lint_healthcheck.py`

**Exports**:
- `run_health_check(vault_root: Path, llm_client: LLMClient, log_fn: Callable[[str], None]) -> None`

---

#### Overview

The health-check uses the identical batching strategy as `analysis.py`:
- Import `ANALYSIS_CONTEXT_WINDOW` and `build_batches()` and `collect_summary_files()` from `analysis.py`.
- Include `index.md` content in every batch (for structural context, per FR-8.3).
- For each batch, send actual summary file content to the LLM asking for four-category findings.
- After all batches, synthesize findings across batches.
- Write `lint-report.md`.

---

#### `run_health_check()` — Main Entry Point

**Signature**:

```python
def run_health_check(
    vault_root: Path,
    llm_client: "LLMClient",
    log_fn: Callable[[str], None],
) -> None:
    """Run the deep vault health-check and write lint-report.md.

    Steps:
      1. Read index.md for structural context.
      2. Collect all summary files via collect_summary_files() from analysis.py.
      3. Build tiktoken batches via build_batches() from analysis.py.
      4. For each batch: send summary content + index.md to LLM for four-category findings.
      5. Synthesize per-batch findings into a unified report.
      6. Write lint-report.md.
      7. Print "Lint report written to lint-report.md".
    """
```

---

#### Step 1 — Read `index.md`

```python
def _read_index_content(vault_root: Path, logger: "logging.Logger") -> str:
    """Read index.md content for inclusion in every health-check batch."""
    index_path = vault_root / "index.md"
    try:
        return index_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Cannot read index.md for health-check: %s", exc)
        return ""
```

---

#### Step 2-3 — Collect and Batch Summary Files

Reuse `collect_summary_files()` and `build_batches()` from `analysis.py`. These are imported directly; the health-check does not duplicate the batching logic.

```python
from codebase_wiki_builder.analysis import (
    ANALYSIS_CONTEXT_WINDOW,
    build_batches,
    collect_summary_files,
)
```

Both `build_batches` and `collect_summary_files` are public functions in `analysis.py`. They are imported here because `lint_healthcheck.py` needs the identical batching algorithm without duplicating logic.

---

#### Step 4 — Per-Batch Health-Check LLM Call

**Per-batch prompt**:

```python
# HEALTH_CHECK_BATCH_PROMPT is kept as documentation only — do NOT use with .format() at runtime.
# Use _build_batch_health_check_prompt() which constructs the prompt via an f-string.
HEALTH_CHECK_BATCH_PROMPT = """\
You are performing a deep health-check of a codebase wiki. Below is the wiki's index (index.md) followed by a batch of summary files from one directory of the vault.

Analyze these pages and identify findings in EXACTLY FOUR categories:

1. ORPHAN PAGES: Query pages or summary pages with no inbound backlinks from any other wiki page.
2. MISSING CROSS-REFERENCES: Pairs of pages that are clearly related by their content but do not link to each other.
3. CONTRADICTIONS: Claims in one page that appear to contradict claims in another page (best-effort based on content).
4. CONCEPT GAPS: Important concepts mentioned across multiple pages but with no dedicated summary page.

For each finding, be specific: name the pages involved.
If you find nothing for a category in this batch, write "None found in this batch."

--- INDEX.MD ---
{index_content}
--- END INDEX ---

--- SUMMARY FILES ---
{combined_summaries}
--- END SUMMARY FILES ---

Format your response as:
## Orphan Pages
[findings or "None found in this batch."]

## Missing Cross-References
[findings or "None found in this batch."]

## Contradictions
[findings or "None found in this batch."]

## Concept Gaps
[findings or "None found in this batch."]
"""


def _build_batch_health_check_prompt(index_content: str, combined_summaries: str) -> str:
    """Build the per-batch health-check prompt using an f-string.

    Uses an f-string rather than HEALTH_CHECK_BATCH_PROMPT.format() so that curly braces
    in untrusted content (index.md, vault summary files) cannot corrupt the prompt or raise
    KeyError at the Python layer.
    """
    return (
        "You are performing a deep health-check of a codebase wiki. Below is the wiki's "
        "index (index.md) followed by a batch of summary files from one directory of the vault.\n"
        "\n"
        "Analyze these pages and identify findings in EXACTLY FOUR categories:\n"
        "\n"
        "1. ORPHAN PAGES: Query pages or summary pages with no inbound backlinks from any other wiki page.\n"
        "2. MISSING CROSS-REFERENCES: Pairs of pages that are clearly related by their content but do not link to each other.\n"
        "3. CONTRADICTIONS: Claims in one page that appear to contradict claims in another page (best-effort based on content).\n"
        "4. CONCEPT GAPS: Important concepts mentioned across multiple pages but with no dedicated summary page.\n"
        "\n"
        "For each finding, be specific: name the pages involved.\n"
        'If you find nothing for a category in this batch, write "None found in this batch."\n'
        "\n"
        "--- INDEX.MD ---\n"
        f"{index_content}\n"
        "--- END INDEX ---\n"
        "\n"
        "--- SUMMARY FILES ---\n"
        f"{combined_summaries}\n"
        "--- END SUMMARY FILES ---\n"
        "\n"
        "Format your response as:\n"
        "## Orphan Pages\n"
        '[findings or "None found in this batch."]\n'
        "\n"
        "## Missing Cross-References\n"
        '[findings or "None found in this batch."]\n'
        "\n"
        "## Contradictions\n"
        '[findings or "None found in this batch."]\n'
        "\n"
        "## Concept Gaps\n"
        '[findings or "None found in this batch."]\n'
    )
```

**Per-batch processing**:

```python
def _run_batch_health_check(
    batch: "AnalysisBatch",
    index_content: str,
    llm_client: "LLMClient",
    logger: "logging.Logger",
) -> str:
    """Send one batch of summary files to the LLM for health-check findings.

    Returns the LLM response text (four-category findings).
    Returns an empty string on LLM error.
    """
    combined = "\n\n---\n\n".join(
        f"File: {path.name}\n\n{content}"
        for path, content in zip(batch.file_paths, batch.contents)
    )
    prompt = _build_batch_health_check_prompt(index_content, combined)

    try:
        response = llm_client.complete(prompt)
        logger.info(
            "Health-check batch for '%s': %d files processed",
            batch.vault_dir or "(root)", len(batch.file_paths),
        )
        return response
    except Exception as exc:
        logger.error("LLM health-check batch failed for '%s': %s", batch.vault_dir, exc)
        return ""
```

---

#### Step 5 — Synthesis

After all batches, collect all per-batch findings and synthesize them into a unified report.

```python
# HEALTH_CHECK_SYNTHESIS_PROMPT is kept as documentation only — do NOT use with .format() at runtime.
# Use _build_health_check_synthesis_prompt() which constructs the prompt via an f-string.
HEALTH_CHECK_SYNTHESIS_PROMPT = """\
You are synthesizing health-check findings from multiple batches of a codebase wiki analysis into a unified report. Each section below contains findings from one directory batch.

Produce a single unified report with EXACTLY FOUR sections. Deduplicate findings that appear in multiple batches. Consolidate related findings. If a category has no findings across all batches, write "None found."

--- PER-BATCH FINDINGS ---
{batch_findings}
--- END FINDINGS ---

Format your response as:
## Orphan Pages
[unified findings or "None found."]

## Missing Cross-References
[unified findings or "None found."]

## Contradictions
[unified findings or "None found."]

## Concept Gaps
[unified findings or "None found."]
"""


def _build_health_check_synthesis_prompt(batch_findings_text: str) -> str:
    """Build the health-check synthesis prompt using an f-string.

    Uses an f-string rather than HEALTH_CHECK_SYNTHESIS_PROMPT.format() so that curly
    braces in untrusted LLM-generated per-batch findings text cannot corrupt the prompt
    or raise KeyError at the Python layer.
    """
    return (
        "You are synthesizing health-check findings from multiple batches of a codebase wiki "
        "analysis into a unified report. Each section below contains findings from one directory batch.\n"
        "\n"
        "Produce a single unified report with EXACTLY FOUR sections. Deduplicate findings that "
        "appear in multiple batches. Consolidate related findings. If a category has no findings "
        'across all batches, write "None found."\n'
        "\n"
        "--- PER-BATCH FINDINGS ---\n"
        f"{batch_findings_text}\n"
        "--- END FINDINGS ---\n"
        "\n"
        "Format your response as:\n"
        "## Orphan Pages\n"
        '[unified findings or "None found."]\n'
        "\n"
        "## Missing Cross-References\n"
        '[unified findings or "None found."]\n'
        "\n"
        "## Contradictions\n"
        '[unified findings or "None found."]\n'
        "\n"
        "## Concept Gaps\n"
        '[unified findings or "None found."]\n'
    )


def _synthesize_health_check(
    batch_findings: list[tuple[str, str]],   # (vault_dir, findings_text)
    llm_client: "LLMClient",
    logger: "logging.Logger",
) -> str:
    """Synthesize per-batch health-check findings into a unified report body.

    Returns the LLM response (four-section text).
    Falls back to concatenating all batch findings if LLM call fails.
    """
    combined_sections = "\n\n---\n\n".join(
        f"Directory: {vault_dir if vault_dir else '(root)'}\n\n{text}"
        for vault_dir, text in batch_findings
        if text.strip()
    )

    if not combined_sections:
        return (
            "## Orphan Pages\nNone found.\n\n"
            "## Missing Cross-References\nNone found.\n\n"
            "## Contradictions\nNone found.\n\n"
            "## Concept Gaps\nNone found."
        )

    prompt = _build_health_check_synthesis_prompt(combined_sections)

    try:
        return llm_client.complete(prompt)
    except Exception as exc:
        logger.error("Health-check synthesis failed: %s", exc)
        # Fallback: concatenate batch findings as-is
        return combined_sections
```

---

#### Step 6 — Write `lint-report.md`

```python
LINT_REPORT_HEADER = """\
# Wiki Lint Report
Generated: {timestamp}

"""

DEDUP_SECTION_PLACEHOLDER = """\
## Deduplicated Query Pages
{dedup_entries}
"""


def _write_lint_report(
    vault_root: Path,
    synthesis: str,
    dedup_entries: list[str],
    logger: "logging.Logger",
) -> None:
    """Write lint-report.md to vault root. Overwrites on each run.

    Args:
        synthesis: The four-section synthesis text from the LLM.
        dedup_entries: List of 'old-page.md → merged-page.md' strings from dedup step.
                       Pass empty list if no deduplication was performed this run.
    """
    from datetime import datetime, timezone

    timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    dedup_content: str
    if dedup_entries:
        dedup_content = "\n".join(f"- {entry}" for entry in dedup_entries)
    else:
        dedup_content = "None"

    dedup_section = DEDUP_SECTION_PLACEHOLDER.format(dedup_entries=dedup_content)

    report_content = (
        LINT_REPORT_HEADER.format(timestamp=timestamp)
        + synthesis.strip()
        + "\n\n"
        + dedup_section
    )

    report_path = vault_root / "lint-report.md"
    try:
        report_path.write_text(report_content, encoding="utf-8")
        logger.info("Wrote lint-report.md")
    except OSError as exc:
        logger.error("Cannot write lint-report.md: %s", exc)
        raise
```

The `dedup_entries` parameter allows the lint CLI (item 17) to pass deduplication results from Part 2 into the `lint-report.md` `## Deduplicated Query Pages` section. The lint CLI assembles these from `LintDedupResult.merged_groups`.

**Design note**: `run_health_check()` accepts an optional `dedup_result` parameter (type `LintDedupResult | None = None`) so the lint CLI can pass Part 2 results. If `None`, the section shows "None".

---

#### Complete `run_health_check()` Body

```python
def run_health_check(
    vault_root: Path,
    llm_client: "LLMClient",
    log_fn: Callable[[str], None],
    dedup_result: "LintDedupResult | None" = None,
) -> None:
    import logging
    from codebase_wiki_builder.analysis import (
        ANALYSIS_CONTEXT_WINDOW,
        build_batches,
        collect_summary_files,
    )

    logger = logging.getLogger(__name__)

    # Step 1: Read index.md for batch context
    index_content = _read_index_content(vault_root, logger)

    # Step 2: Collect summary files
    summary_files = collect_summary_files(vault_root)
    logger.info("Health-check: found %d summary files", len(summary_files))

    # If no summary files, write a minimal report
    if not summary_files:
        logger.warning("No summary files found for health-check")
        _write_lint_report(
            vault_root,
            (
                "## Orphan Pages\nNone found.\n\n"
                "## Missing Cross-References\nNone found.\n\n"
                "## Contradictions\nNone found.\n\n"
                "## Concept Gaps\nNone found."
            ),
            dedup_entries=[],
            logger=logger,
        )
        import typer
        typer.echo("Lint report written to lint-report.md")
        return

    # Step 3: Build tiktoken batches (identical to analysis command)
    batches = build_batches(summary_files, vault_root, logger)
    logger.info("Health-check: built %d batch(es)", len(batches))

    # Step 4: Per-batch health-check
    batch_findings: list[tuple[str, str]] = []
    for batch in batches:
        findings_text = _run_batch_health_check(batch, index_content, llm_client, logger)
        batch_findings.append((batch.vault_dir, findings_text))

    # Step 5: Synthesize findings
    synthesis = _synthesize_health_check(batch_findings, llm_client, logger)

    # Prepare dedup entries for the report
    dedup_entries: list[str] = []
    if dedup_result:
        for surviving_path, deleted_paths in dedup_result.merged_groups:
            surviving_rel = surviving_path.relative_to(vault_root).as_posix()
            for deleted_path in deleted_paths:
                deleted_rel = deleted_path.relative_to(vault_root).as_posix()
                dedup_entries.append(f"{deleted_rel} → {surviving_rel}")

    # Step 6: Write lint-report.md
    _write_lint_report(vault_root, synthesis, dedup_entries, logger)

    import typer
    typer.echo("Lint report written to lint-report.md")
```

---

#### Complete Module Skeleton for `lint_healthcheck.py`

```python
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from codebase_wiki_builder.lint_dedup import LintDedupResult
    from codebase_wiki_builder.llm_client import LLMClient
    from codebase_wiki_builder.analysis import AnalysisBatch

logger = logging.getLogger(__name__)

# Prompts (module-level constants)
HEALTH_CHECK_BATCH_PROMPT: str = ...
HEALTH_CHECK_SYNTHESIS_PROMPT: str = ...
LINT_REPORT_HEADER: str = ...

# Public entry point
def run_health_check(
    vault_root: Path,
    llm_client: "LLMClient",
    log_fn: Callable[[str], None],
    dedup_result: "LintDedupResult | None" = None,
) -> None: ...

# Internal helpers
def _read_index_content(vault_root: Path, logger: logging.Logger) -> str: ...
def _run_batch_health_check(
    batch: "AnalysisBatch",
    index_content: str,
    llm_client: "LLMClient",
    logger: logging.Logger,
) -> str: ...
def _synthesize_health_check(
    batch_findings: list[tuple[str, str]],
    llm_client: "LLMClient",
    logger: logging.Logger,
) -> str: ...
def _write_lint_report(
    vault_root: Path,
    synthesis: str,
    dedup_entries: list[str],
    logger: logging.Logger,
) -> None: ...
```

---

## Error Handling

| Condition | Module | Behavior |
|-----------|--------|----------|
| `index.md` missing when collecting query entries | `lint_dedup.py` | `_collect_query_entries()` returns `[]`; dedup is a no-op ("No duplicate query pages found.") |
| Fewer than 2 query pages | `lint_dedup.py` | Skip detection pass; return empty result |
| LLM detection pass fails or returns invalid JSON | `lint_dedup.py` | Logged at WARNING; `_run_detection_pass()` returns `[]`; dedup skipped |
| LLM detection returns group with unknown filename | `lint_dedup.py` | Logged at WARNING; that entry skipped; rest of group processed if ≥2 readable pages remain |
| Page in duplicate group not found on disk | `lint_dedup.py` | Added to `skipped_pages`; group skipped if fewer than 2 readable pages remain |
| `read_query_page()` fails for a group page | `lint_dedup.py` | Logged at ERROR; added to `skipped_pages`; group skipped if count drops below 2 |
| LLM merge pass fails | `lint_dedup.py` | Logged at ERROR; `_run_merge_pass()` falls back to surviving page content |
| `write_text()` fails when writing merged page | `lint_dedup.py` | Logged at ERROR; all pages in group added to `skipped_pages`; continue to next group |
| `unlink()` fails when deleting a merged-away page | `lint_dedup.py` | Logged at ERROR; added to `skipped_pages`; surviving page and index still updated |
| `index.md` not writable after merge | `lint_dedup.py` | Logged at ERROR; page files already written; index inconsistent until next ingest |
| No summary files for health-check | `lint_healthcheck.py` | Minimal report written (all sections "None found."); no LLM calls |
| `index.md` unreadable | `lint_healthcheck.py` | Logged at WARNING; empty string used for index context in batches; continues |
| LLM batch health-check call fails | `lint_healthcheck.py` | Logged at ERROR; empty string returned for that batch; synthesis continues with remaining batches |
| LLM synthesis call fails | `lint_healthcheck.py` | Logged at ERROR; fallback: concatenate all batch findings as-is |
| `lint-report.md` write fails | `lint_healthcheck.py` | `OSError` propagates to lint CLI (item 17); lint command exits with code 1 |

---

## Unit Test Specifications

**Files**: `tests/test_lint_dedup.py`, `tests/test_lint_healthcheck.py`

All tests use `tmp_path`. LLM calls are mocked. No real network calls.

---

### `lint_dedup.py` — `_collect_query_entries()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| No index.md | Fresh vault | Returns `[]` | Graceful |
| No query rows | `index.md` with only summary rows | Returns `[]` | Filters non-query entries |
| One query row | Row with `[[queries/how-auth-works]]` | Returns one entry with correct wikilink, description, path | Happy path |
| Multiple query rows | Three query rows | Returns three entries in row order | Row order preserved |
| Stale annotation stripped | Row description has `⚠ stale` | Entry description has annotation removed | Clean description for LLM |
| Non-query row excluded | Row with `[[src/auth.py]]` | Not in results | Only `queries/` prefix included |

---

### `lint_dedup.py` — `_run_detection_pass()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| LLM returns empty array | Mock returns `"[]"` | Returns `[]` | No duplicates |
| LLM returns valid groups | Mock returns `'[["queries/a.md", "queries/b.md"]]'` | Returns one group with two filenames | Happy path |
| LLM returns invalid JSON | Mock returns `"not json"` | Returns `[]` (logged WARNING) | JSON parse failure |
| LLM raises exception | Mock raises `LLMError` | Returns `[]` (logged ERROR) | API failure |
| Group with only one element filtered | `[["queries/a.md"]]` | Returns `[]` (group too small) | Minimum 2 elements per group |
| Response with extra text before `[` | `"Sure, here: [...]"` | JSON array extracted correctly | LLM prose preamble handled |

---

### `lint_dedup.py` — `_most_recent_page()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Both have `updated_at` | Page A: `2026-04-01`, Page B: `2026-04-30` | Returns Page B | Most recent `updated_at` wins |
| `updated_at` absent; use `saved_at` | Page A: no `updated_at`, `saved_at: 2026-04-01`; Page B: all absent | Returns Page A | `saved_at` fallback |
| Both absent; use row order | Both pages have no timestamps | Returns page with higher `row_index` | Row order tiebreaker |
| Three pages; middle is most recent | A: `2026-01`, B: `2026-04`, C: `2026-02` | Returns B | Max among three |

---

### `lint_dedup.py` — `_write_merged_page()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| `saved_at` preserved | Surviving page has `saved_at: 2026-01-01` | Written file has same `saved_at` | AT-16(b): saved_at preserved |
| `updated_at` set to now | Any surviving page | Written file has `updated_at` matching current timestamp | AT-16(b) |
| `## Page Metadata` section present | Any inputs | Written file ends with `## Page Metadata` section | FR-8.2(d) |
| Merged body in file | `merged_body = "# Question\n\nAnswer."` | Written file starts with that content | Content preserved |

---

### `lint_dedup.py` — `deduplicate_query_pages()` — integration (AT-16)

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Fewer than 2 pages | One query page in index | "No duplicate query pages found."; empty result | FR-8.2 step 2 |
| No duplicates found | Mock LLM returns `[]` | "No duplicate query pages found."; empty result | No duplicates |
| One duplicate group merged | Two query pages; mock LLM returns group; mock merge returns content | Surviving file overwritten; other deleted; index has one row; log entry written | AT-16(a-d) |
| Correct file deleted | Two pages: A (older) and B (newer); B survives | A deleted from disk; B has merged content | AT-16(a) |
| `saved_at` from surviving page | B has `saved_at: 2026-01-01` | Merged file has `saved_at: 2026-01-01` | AT-16(b) |
| `updated_at` is current timestamp | Any merge | Merged file has `updated_at` matching current run time | AT-16(b) |
| index.md has one row after merge | Two rows before; one group merged | index.md has exactly one row for the merged page | AT-16(c) |
| `lint-deduplicated` log entry | One merge | `log_fn` called with entry containing `lint-deduplicated` and both filenames | AT-16(d) |
| Terminal shows `✓ Merged:` | One merge | Output contains `✓ Merged:` with both filenames | AT-16(e) |

**Key Scenario: Full deduplication flow (AT-16)**

```python
def test_dedup_full_flow(tmp_path):
    from unittest.mock import MagicMock, patch
    from codebase_wiki_builder.lint_dedup import deduplicate_query_pages

    vault = tmp_path / "vault"
    vault.mkdir()
    queries_dir = vault / "queries"
    queries_dir.mkdir()

    # Create two near-duplicate query pages
    page_a = queries_dir / "how-does-auth-work.md"
    page_b = queries_dir / "explain-authentication.md"

    page_a.write_text(
        "# How does auth work?\n\nAuth uses JWT.\n\n"
        "## Sources\n- src/auth.py.md\n\n"
        "## Page Metadata\n"
        "saved_at: 2026-04-01 10:00:00 UTC\n"
        "updated_at: 2026-04-01 10:00:00 UTC\n"
    )
    page_b.write_text(
        "# Explain authentication\n\nAuthentication is handled by JWT tokens.\n\n"
        "## Sources\n- src/auth.py.md\n\n"
        "## Page Metadata\n"
        "saved_at: 2026-04-29 10:00:00 UTC\n"
        "updated_at: 2026-04-29 10:00:00 UTC\n"
    )

    (vault / "index.md").write_text(
        "| File | Description |\n|------|-------------|\n"
        "| [[queries/how-does-auth-work]] | Explains how authentication works |\n"
        "| [[queries/explain-authentication]] | Describes the authentication system |\n"
    )

    log_entries = []
    llm_client = MagicMock()

    # Detection returns one group
    llm_client.complete.side_effect = [
        '[ ["queries/how-does-auth-work.md", "queries/explain-authentication.md"] ]',  # detection
        "# Explain authentication\n\nAuthentication uses JWT tokens for all requests.\n\n## Sources\n- src/auth.py.md",  # merge
    ]

    result = deduplicate_query_pages(vault, llm_client, log_entries.append)

    # (a) One file deleted
    assert not page_a.exists()   # older page (lower updated_at) deleted
    assert page_b.exists()        # newer page (higher updated_at) survives

    # (b) Surviving file has correct metadata
    content = page_b.read_text(encoding="utf-8")
    assert "saved_at: 2026-04-29 10:00:00 UTC" in content  # saved_at preserved
    assert "updated_at: 2026-04-29 10:00:00 UTC" not in content  # updated_at refreshed

    # (c) index.md has exactly one row for merged page
    index_content = (vault / "index.md").read_text(encoding="utf-8")
    assert "[[queries/how-does-auth-work]]" not in index_content
    assert "[[queries/explain-authentication]]" in index_content

    # (d) lint-deduplicated log entry
    log_text = "\n".join(log_entries)
    assert "lint-deduplicated" in log_text
    assert "how-does-auth-work.md" in log_text
    assert "explain-authentication.md" in log_text

    # Result structure
    assert len(result.merged_groups) == 1
    surviving, deleted_list = result.merged_groups[0]
    assert surviving == page_b
    assert page_a in deleted_list
```

---

### `lint_healthcheck.py` — `run_health_check()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| No summary files | Empty vault | `lint-report.md` written with all four sections showing "None found." | Graceful empty case |
| Standard run | Summary files exist; mock LLM | `lint-report.md` exists with all four section headers | AT-17 |
| All four section headers present | Any successful run | `## Orphan Pages`, `## Missing Cross-References`, `## Contradictions`, `## Concept Gaps` all in report | AT-17 |
| `## Deduplicated Query Pages` section present | Any run | Report has this section | FR-8.3 format |
| Dedup entries in report | `dedup_result` with one merged group | Section lists the merge | FR-8.3 |
| No dedup entries | `dedup_result=None` | Section shows "None" | FR-8.3 |
| `lint-report.md` overwritten | Existing `lint-report.md` | File overwritten with new content | FR-8.3: overwrite on each run |
| Terminal output | Any run | Prints "Lint report written to lint-report.md" | FR-8.3 |
| Generated timestamp in header | Any run | Report header contains `Generated:` with UTC timestamp | FR-8.3 format |

**Key Scenario: All four section headers present (AT-17)**

```python
def test_health_check_four_sections(tmp_path):
    from unittest.mock import MagicMock, patch
    from codebase_wiki_builder.lint_healthcheck import run_health_check

    vault = tmp_path / "vault"
    vault.mkdir()
    src_dir = vault / "src"
    src_dir.mkdir()

    (vault / "index.md").write_text(
        "| File | Description |\n|------|-------------|\n"
        "| [[src/main.py]] | Main module |\n"
    )
    (src_dir / "main.py.md").write_text(
        "# src/main.py\n\nEntry point.\n\n<!-- md5: abc123 -->\n"
    )

    llm_client = MagicMock()
    llm_client.complete.return_value = (
        "## Orphan Pages\nNone found.\n\n"
        "## Missing Cross-References\nNone found.\n\n"
        "## Contradictions\nNone found.\n\n"
        "## Concept Gaps\nNone found."
    )

    log_entries = []

    with patch("codebase_wiki_builder.lint_healthcheck.build_batches") as mock_batches:
        from codebase_wiki_builder.analysis import AnalysisBatch
        mock_batch = MagicMock(spec=AnalysisBatch)
        mock_batch.vault_dir = "src"
        mock_batch.file_paths = [src_dir / "main.py.md"]
        mock_batch.contents = ["# src/main.py\n\nEntry point."]
        mock_batch.token_count = 50
        mock_batches.return_value = [mock_batch]

        with patch("codebase_wiki_builder.lint_healthcheck.collect_summary_files",
                   return_value=[("src", src_dir / "main.py.md")]):
            run_health_check(vault, llm_client, log_entries.append)

    report = (vault / "lint-report.md").read_text(encoding="utf-8")
    assert "## Orphan Pages" in report
    assert "## Missing Cross-References" in report
    assert "## Contradictions" in report
    assert "## Concept Gaps" in report
    assert "## Deduplicated Query Pages" in report
    assert "Generated:" in report
```

---

### `lint_healthcheck.py` — `_run_batch_health_check()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| LLM returns findings | Mock returns four-section text | Returns that text | Happy path |
| LLM raises exception | Mock raises | Returns `""` (logged ERROR) | Graceful degradation |
| `index.md` included in prompt | Check prompt construction | Prompt contains index content | FR-8.3: index in every batch |

---

### `lint_healthcheck.py` — `_synthesize_health_check()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Single batch | One batch findings | LLM called with synthesis prompt | Single batch still synthesized |
| Multiple batches | Three batch findings | LLM receives all three | Standard case |
| All empty batch findings | All batch responses are `""` | Returns default four-section "None found." text | No LLM call needed |
| LLM synthesis fails | Mock raises | Returns concatenated batch findings as fallback | Graceful |

---

## Notes

- **Detection pass uses titles + descriptions ONLY (spec constraint FR-8.2)**: `_run_detection_pass()` does not read any page file — it uses only the `description` field from `_QueryPageEntry`, which comes from `index.md`. Full content reading happens only in step 4a (`_run_merge_pass()`), after a group is confirmed as duplicate.

- **`build_batches()` and `collect_summary_files()` are public functions in `analysis.py`**: These helpers are defined as public functions in `analysis.py` (no `_` prefix). They are imported directly by `lint_healthcheck.py` because the health-check uses the identical batching algorithm as the analysis command. This avoids duplicating the batching logic and keeps `analysis.py` as the single authoritative source of the tiktoken-based subdivision strategy.

- **`ANALYSIS_CONTEXT_WINDOW` imported from `analysis.py`**: Per the notes in the analysis plan (item 10): "This constant is also referenced by the lint health-check (item 16). It lives here as the authoritative source; item 16 imports it." This import is clean and intended.

- **`run_health_check()` accepts `dedup_result` parameter**: The lint CLI (item 17) calls `deduplicate_query_pages()` first, then passes the resulting `LintDedupResult` to `run_health_check()`. This allows the `## Deduplicated Query Pages` section of `lint-report.md` to reflect what happened in Part 2 of the same lint run. If called standalone (e.g., in tests), `dedup_result=None` is safe.

- **No LLM retry logic in these modules**: The `LLMClient` (item 3) handles retry/backoff internally. The health-check and dedup modules call `llm_client.complete()` and either let `LLMError` propagate (fatal) or catch generic exceptions for graceful degradation (detection pass, batch health-check). Fatal LLM errors during the merge pass or synthesis propagate to the lint CLI which exits with code 1.

- **Dedup skips pages with missing `## Page Metadata`**: `read_query_page()` (item 12) returns `saved_at=""` and `updated_at=""` if the metadata section is absent. `_parse_timestamp("")` returns `None`. The recency key falls back to `row_index`. This handles edge cases gracefully without crashing.

- **`lint-report.md` `## Deduplicated Query Pages` section**: The spec's `lint-report.md` format (FR-8.3) includes this section. The dedup entries come from `LintDedupResult.merged_groups`, which is populated by `lint_dedup.py` and passed into `run_health_check()` by the lint CLI.

- **The `_console` (rich Console) is module-level in `lint_dedup.py`**: Follows the established pattern from `lint_staleness.py`. Terminal output uses `rich` markup for color.

- **Index update in dedup is surgical, not a full rebuild**: `_update_index_for_merge()` reads and rewrites `index.md` in place, replacing group rows with one merged row. A full rebuild is not appropriate here — it would require re-reading all vault files and re-extracting descriptions. The next `ingest` run will fully rebuild `index.md` anyway.

- **Token budget for synthesis (health-check)**: The synthesis step in `_synthesize_health_check()` sends all per-batch findings text to the LLM. In theory, findings from many batches could exceed the context window. For MVP this is left as-is (the findings text is much smaller than the original summary content, so overflow is unlikely). A future version could add tiktoken-based truncation of findings text if needed.
