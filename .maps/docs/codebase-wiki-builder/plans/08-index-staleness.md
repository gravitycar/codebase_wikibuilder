# Implementation Plan: Index Regeneration and Staleness Detection

## Spec Context

This plan implements two tightly coupled Phase 2 responsibilities that run after all summary writes and deletions are complete. `rebuild_index()` regenerates `index.md` as a complete two-column markdown table covering every current wiki page (summary files and `queries/` pages). `detect_stale_queries()` then scans each `queries/` page, parses its `## Sources` section against the Phase 1 change-set (which includes deleted summary paths), inserts a stale banner immediately after the H1 title when sources have changed, annotates the corresponding `index.md` row, and logs each flagged page.

Catalog item: 8 — Index Regeneration (`index.md`) and Staleness Detection
Specification section: FR-3.6 (index format, complete rebuild, query pages preserved), FR-3.8 (staleness detection: Phase 1 change-set, banner placement after H1, duplicate-banner prevention, `## Sources` hard error, `query-stale` log, terminal summary)
Acceptance criteria addressed: AT-22 (banner placement after H1), AT-23 (no duplicate banner), AT-24 (missing Sources hard error), FR-3.6 (complete two-column table, all page types included), FR-3.8 (all staleness detection steps)

## Dependencies

- **Blocked by**:
  - Item 4 (Vault File Utilities + Logging) — needs `wikilink()`, `append_log_md()`, vault walk exclusion constants
  - Item 5 (Scanner) — needs `ChangeSet` dataclass (specifically `deleted_summaries`, `new_files`, `modified_files`)
  - Item 7 (Deletion) — deletions must be applied before index rebuild so index reflects the post-deletion state
- **Blocks**: Items 9, 10, 11, 12 — all downstream components expect `index.md` to exist and be accurate after every ingest
- **Uses**: `pathlib` (stdlib), `re` (stdlib), `os` (stdlib), `logging` (stdlib), `datetime` (stdlib); `ChangeSet` from `scanner.py`; `wikilink()` from `vault.py`; `append_log_md()` from `logging_setup.py`

## File Changes

### New Files

- `codebase_wiki_builder/index_writer.py` — `rebuild_index(vault_root, logger) -> None`; reads all summary files and `queries/` pages, writes the two-column `index.md` table
- `codebase_wiki_builder/staleness.py` — `detect_stale_queries(change_set, vault_root, log_fn, logger) -> StalenessResult`; parses `## Sources`, checks change-set, inserts stale banners, annotates index rows, logs

### Modified Files

- None (both are new modules)

---

## Implementation Details

---

### `index_writer.py`

**File**: `codebase_wiki_builder/index_writer.py`

**Exports**:
- `rebuild_index(vault_root: Path, logger: logging.Logger) -> None` — completely rewrites `index.md` as a two-column table of all current wiki pages

---

#### Data Gathering: What Goes in the Table

The index table must include every current wiki page of three types:

1. **Summary files** — all `.md` files in the vault that are source-file summaries (same exclusion rules as the scanner's vault walk: not `index.md`, `log.md`, `overview.md`, `lint-report.md`; not any `overview.md` in any subdirectory; not under `logs/` or `queries/`)
2. **Overview files** — `overview.md` at the vault root AND any `overview.md` in any subdirectory (these are produced by the `analysis` command and must be preserved across ingest runs)
3. **Query pages** — all `.md` files under `queries/`

For each entry, the table needs two columns:
- **File**: an Obsidian wikilink to the page (using `wikilink()` from `vault.py`)
- **Description**: a one-line description extracted from the page

**Description extraction rules**:
- For summary files: extract the first non-empty line after the H1 title that is not part of a code block. In practice, the LLM summary starts immediately after the title. Use: read lines, skip the first `# ...` line and any blank lines immediately following it, return the first non-blank, non-heading line found. Truncate to 120 characters if longer.
- For `overview.md` files: use a fixed description indicating it is an overview — for the root `overview.md`: `"Top-level application overview"`. For a subdirectory `overview.md` (e.g., `src/auth/overview.md`): `"Directory overview: src/auth/"`. The relative directory path is derived from the file's location relative to vault root.
- For query pages: same first-paragraph extraction as summary files (the H1 is the question, first body paragraph is the answer start). However, the query page's one-line description is stored in `index.md` itself (added by `save_query_page()` in item 12). This means `rebuild_index()` must carry forward the existing description for query pages rather than re-extracting it — see the "Preserving existing query page descriptions" section below.

---

#### Preserving Existing Query Page Descriptions and Stale Annotations

When `rebuild_index()` is called, there may already be an `index.md` from a previous run. That file contains:
- One-line descriptions for query pages (set by `save_query_page()`, not extractable from the page content reliably)
- Stale annotations (` ⚠ stale`) for query pages that were previously flagged

`rebuild_index()` must carry these forward. The strategy:

1. **Before rewriting**: read the existing `index.md` and parse its table rows to build a `dict[str, str]` mapping the wikilink target (the path inside `[[...]]`) to the full Description column value (which may include ` ⚠ stale`).
2. **When building the new table**: for any wiki page whose wikilink target already has a Description entry in the old index, reuse that description (preserving stale annotations). For pages with no prior entry (new pages, new query pages), generate descriptions fresh.

This approach ensures that:
- `detect_stale_queries()` (called after `rebuild_index()`) can find and annotate query page rows by their wikilink string
- Stale annotations from a prior ingest that are still valid persist across the rebuild

**Note**: `detect_stale_queries()` is responsible for ADDING stale annotations after the index is rebuilt. `rebuild_index()` only preserves annotations that were already in the old index; it does not evaluate staleness itself.

---

#### `rebuild_index()` Implementation

```python
from codebase_wiki_builder.vault import VAULT_SPECIAL_FILES, VAULT_EXCLUDED_DIRS

INDEX_FILENAME = "index.md"


def rebuild_index(vault_root: Path, logger: logging.Logger) -> None:
    """Completely rewrite index.md as a two-column markdown table.

    Covers:
    - All source-file summary pages (vault-mirrored .md files)
    - All overview.md files (root and subdirectory)
    - All query pages under queries/
    """
    # Step 1: Read old index to carry forward query descriptions and stale annotations
    old_descriptions = _parse_existing_index(vault_root)

    # Step 2: Collect all wiki pages
    pages = _collect_summary_pages(vault_root)        # source summaries
    overviews = _collect_overview_pages(vault_root)   # overview.md files
    query_pages = _collect_query_pages(vault_root)    # queries/*.md

    # Step 3: Build table rows
    rows: list[tuple[str, str]] = []
    for page_path in sorted(pages):
        link = wikilink(page_path, vault_root)
        desc = old_descriptions.get(_wikilink_target(link)) or _extract_description(page_path)
        rows.append((link, desc))

    for page_path in sorted(overviews):
        link = wikilink(page_path, vault_root)
        desc = old_descriptions.get(_wikilink_target(link)) or _overview_description(page_path, vault_root)
        rows.append((link, desc))

    for page_path in sorted(query_pages):
        link = wikilink(page_path, vault_root)
        desc = old_descriptions.get(_wikilink_target(link)) or _extract_description(page_path)
        rows.append((link, desc))

    # Step 4: Write index.md
    _write_index(vault_root, rows, logger)
```

---

#### Internal Helpers

**`_parse_existing_index(vault_root: Path) -> dict[str, str]`**

Reads the existing `index.md` and returns a mapping of wikilink target → Description. Returns empty dict if `index.md` does not exist or has no table.

The index table format is:

```markdown
| File | Description |
|------|-------------|
| [[path/to/file]] | One-line description here |
```

Parse by finding lines that start with `|` and contain `[[`. Extract the content of the first `[[...]]` group as the key, and the second pipe-separated column as the value (stripped).

```python
import re

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_TABLE_ROW_RE = re.compile(r"^\|\s*(\[\[.*?\]\])\s*\|\s*(.*?)\s*\|$")


def _parse_existing_index(vault_root: Path) -> dict[str, str]:
    index_path = vault_root / INDEX_FILENAME
    if not index_path.exists():
        return {}
    try:
        content = index_path.read_text(encoding="utf-8")
    except OSError:
        return {}

    result: dict[str, str] = {}
    for line in content.splitlines():
        m = _TABLE_ROW_RE.match(line.strip())
        if m:
            link_cell = m.group(1).strip()      # e.g. [[src/auth/login.py]]
            desc_cell = m.group(2).strip()       # e.g. "Handles login logic ⚠ stale"
            inner = _WIKILINK_RE.search(link_cell)
            if inner:
                result[inner.group(1)] = desc_cell
    return result
```

**`_wikilink_target(link: str) -> str`**

Extracts the path inside a `[[...]]` string. Example: `"[[src/auth/login.py]]"` → `"src/auth/login.py"`.

```python
def _wikilink_target(link: str) -> str:
    m = _WIKILINK_RE.search(link)
    return m.group(1) if m else link
```

**`_collect_summary_pages(vault_root: Path) -> list[Path]`**

Walks the vault, applying the same exclusion rules as `_collect_remaining_summaries()` in `deletion.py`. Returns list of absolute paths of all source-file summary `.md` files.

Uses `os.walk()` with `dirnames[:] = [d for d in dirnames if d not in VAULT_EXCLUDED_DIRS]` pruning. Excludes `VAULT_SPECIAL_FILES` at vault root (imported from `vault.py`). Excludes any file named `overview.md`. Only returns files ending in `.md`.

**`_collect_overview_pages(vault_root: Path) -> list[Path]`**

Returns any `overview.md` files found in the vault (root-level `overview.md` and any subdirectory `overview.md` files under non-excluded directories). Uses `os.walk()` with `logs/` and `queries/` pruned.

**`_collect_query_pages(vault_root: Path) -> list[Path]`**

Returns all `.md` files directly under `vault_root / "queries"`. Returns empty list if `queries/` does not exist.

```python
def _collect_query_pages(vault_root: Path) -> list[Path]:
    queries_dir = vault_root / "queries"
    if not queries_dir.is_dir():
        return []
    return [p for p in queries_dir.iterdir() if p.is_file() and p.suffix == ".md"]
```

**`_extract_description(page_path: Path) -> str`**

Reads a page file and extracts a one-line description. Algorithm:
1. Read all lines.
2. Skip the first line if it starts with `# ` (the H1 title).
3. Skip blank lines.
4. Return the first non-blank, non-heading line, stripped, truncated to 120 characters.
5. If no such line found, return `"(no description)"`.

```python
def _extract_description(page_path: Path) -> str:
    try:
        lines = page_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "(no description)"

    skipped_h1 = False
    for line in lines:
        stripped = line.strip()
        if not skipped_h1 and stripped.startswith("# "):
            skipped_h1 = True
            continue
        if not stripped:
            continue
        if stripped.startswith("#"):
            # Hit a section heading — stop
            break
        return stripped[:120]
    return "(no description)"
```

**`_overview_description(page_path: Path, vault_root: Path) -> str`**

Returns the appropriate description string for an `overview.md` file:
- If `page_path == vault_root / "overview.md"`: returns `"Top-level application overview"`
- Otherwise: compute relative directory path. E.g. `vault_root/src/auth/overview.md` → `"Directory overview: src/auth/"`

```python
def _overview_description(page_path: Path, vault_root: Path) -> str:
    if page_path.parent == vault_root:
        return "Top-level application overview"
    rel_dir = page_path.parent.relative_to(vault_root)
    return f"Directory overview: {rel_dir.as_posix()}/"
```

**`_write_index(vault_root: Path, rows: list[tuple[str, str]], logger: logging.Logger) -> None`**

Writes the complete `index.md` file. Uses a two-column markdown table with `| File | Description |` header.

```python
def _write_index(
    vault_root: Path,
    rows: list[tuple[str, str]],
    logger: logging.Logger,
) -> None:
    index_path = vault_root / INDEX_FILENAME
    lines = [
        "| File | Description |",
        "|------|-------------|",
    ]
    for link, desc in rows:
        # Escape pipe characters in description to avoid breaking table formatting
        safe_desc = desc.replace("|", "\\|")
        lines.append(f"| {link} | {safe_desc} |")

    content = "\n".join(lines) + "\n"
    index_path.write_text(content, encoding="utf-8")
    logger.info("index.md rebuilt with %d entries", len(rows))
```

---

### `staleness.py`

**File**: `codebase_wiki_builder/staleness.py`

**Exports**:
- `StalenessResult` — dataclass with results of the staleness detection pass
- `detect_stale_queries(change_set: ChangeSet, vault_root: Path, log_fn: Callable[[str], None], logger: logging.Logger) -> StalenessResult` — main entry point

---

#### `StalenessResult` Dataclass

```python
from dataclasses import dataclass, field
from pathlib import Path


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
```

The CLI (item 9) uses `flagged_pages` and `malformed_sources_pages` for the terminal summary.

---

#### Change-Set to Vault-Path Set Conversion

The staleness check compares `## Sources` entries against the full Phase 1 change-set. `detect_stale_queries()` accepts the raw `ChangeSet` directly and derives the set of changed vault-relative paths internally using `vault_path_for_source()` from `vault.py`.

The `## Sources` section in a query page lists entries as:
```
## Sources
- src/auth/login.py.md
- src/utils/helper.py.md (too large to include)
```

These are vault-relative paths (relative to vault root). The change-set contains:
- `change_set.new_files` — source file absolute paths (Phase 1 new)
- `change_set.modified_files` — source file absolute paths (Phase 1 modified)
- `change_set.deleted_summaries` — vault summary absolute paths (Phase 1 deleted)

`detect_stale_queries()` internally calls `vault_path_for_source()` to convert new/modified source paths to vault summary paths, then combines them with `deleted_summaries` into a flat `set[str]` of vault-relative path strings for comparison.

**Signature**:

```python
def detect_stale_queries(
    change_set: ChangeSet,           # raw Phase 1 result from scan_codebase()
    vault_root: Path,
    log_fn: Callable[[str], None],
    logger: logging.Logger,
) -> StalenessResult:
```

**Internal path extraction** (at the top of the function body):

```python
from codebase_wiki_builder.vault import vault_path_for_source
from codebase_wiki_builder.scanner import ChangeSet

changed_vault_paths: set[str] = set()

# Deleted summaries are already vault absolute paths → convert to vault-relative strings
for vault_summary_path in change_set.deleted_summaries:
    try:
        rel = vault_summary_path.relative_to(vault_root)
        changed_vault_paths.add(rel.as_posix())
    except ValueError:
        logger.warning("Could not relativize deleted summary path: %s", vault_summary_path)

# New/modified source files → compute their vault summary paths → convert to vault-relative strings
# Note: vault_path_for_source needs codebase_root; we derive it from the first source file
# if available, since all source files share the same codebase root as configured.
# The codebase root is not stored in ChangeSet directly; instead, we compute it path-relative:
# for each source file, its vault summary path is stored in deleted_summaries for deleted files,
# and for new/modified files we re-derive the vault path using the vault_root relationship.
# Simpler: accept codebase_root as a parameter (see revised signature below).
```

**Revised signature** (including `codebase_root` to enable `vault_path_for_source()` calls):

```python
def detect_stale_queries(
    change_set: ChangeSet,
    vault_root: Path,
    codebase_root: Path,
    log_fn: Callable[[str], None],
    logger: logging.Logger,
) -> StalenessResult:
```

The `codebase_root` is available to the ingest CLI (item 9) from `config.codebase_path`. The internal path conversion becomes:

```python
changed_vault_paths: set[str] = set()

for vault_summary_path in change_set.deleted_summaries:
    try:
        rel = vault_summary_path.relative_to(vault_root)
        changed_vault_paths.add(rel.as_posix())
    except ValueError:
        logger.warning("Could not relativize deleted summary path: %s", vault_summary_path)

for source_file in change_set.new_files + change_set.modified_files:
    try:
        vault_summary_path = vault_path_for_source(source_file, codebase_root, vault_root)
        rel = vault_summary_path.relative_to(vault_root)
        changed_vault_paths.add(rel.as_posix())
    except (ValueError, Exception) as exc:
        logger.warning("Could not compute vault path for source %s: %s", source_file, exc)
```

This keeps the path-conversion logic inside `staleness.py` rather than duplicating it in the CLI, and makes `detect_stale_queries()` self-contained.

---

#### `detect_stale_queries()` Algorithm

```
1. Collect all query pages: list all .md files under vault_root/queries/
2. If no query pages exist: return empty StalenessResult (no-op per FR-3.8 step 6)
3. For each query page:
   a. Parse ## Sources section → list of source paths
   b. If ## Sources missing or malformed → record as malformed_sources error; continue
   c. Check if already stale → scan for existing stale banner line
   d. If already stale → add to already_stale_pages; skip (no duplicate banner)
   e. Check if any source path in changed_vault_paths → flag stale or clean
   f. If stale: insert banner after H1; annotate index.md row; log query-stale entry
4. Return StalenessResult
```

```python
def detect_stale_queries(
    change_set: ChangeSet,
    vault_root: Path,
    codebase_root: Path,
    log_fn: Callable[[str], None],
    logger: logging.Logger,
) -> StalenessResult:
    from codebase_wiki_builder.vault import vault_path_for_source

    # Build changed_vault_paths set internally from the raw ChangeSet
    changed_vault_paths: set[str] = set()
    for vault_summary_path in change_set.deleted_summaries:
        try:
            rel = vault_summary_path.relative_to(vault_root)
            changed_vault_paths.add(rel.as_posix())
        except ValueError:
            logger.warning("Could not relativize deleted summary path: %s", vault_summary_path)
    for source_file in change_set.new_files + change_set.modified_files:
        try:
            vault_summary_path = vault_path_for_source(source_file, codebase_root, vault_root)
            rel = vault_summary_path.relative_to(vault_root)
            changed_vault_paths.add(rel.as_posix())
        except (ValueError, Exception) as exc:
            logger.warning("Could not compute vault path for source %s: %s", source_file, exc)

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
```

---

#### `_process_query_page()` Internal Helper

```python
def _process_query_page(
    query_page: Path,
    changed_vault_paths: set[str],
    vault_root: Path,
    log_fn: Callable[[str], None],
    logger: logging.Logger,
    result: StalenessResult,
) -> None:
```

**Step 1 — Read and parse the query page**

```python
    try:
        content = query_page.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Cannot read query page %s: %s", query_page, exc)
        result.malformed_sources_pages.append(query_page)
        return
```

**Step 2 — Parse `## Sources` section**

Use `_parse_sources_section(content)` to extract the list of source paths. Returns `list[str] | None`:
- `None` → section missing
- `[]` → section present but empty (malformed)
- `["src/auth/login.py.md", ...]` → well-formed

If the result is `None` or `[]`, this is a hard error per FR-3.8:

```python
    sources = _parse_sources_section(content)
    if sources is None:
        logger.error(
            "Query page %s has no ## Sources section (hard error)", query_page.name
        )
        ts = _utc_now()
        log_fn(f"{ts} | sources-error | {query_page.relative_to(vault_root).as_posix()} (missing ## Sources section)")
        result.malformed_sources_pages.append(query_page)
        return
    if not sources:
        logger.error(
            "Query page %s has an empty/malformed ## Sources section (hard error)", query_page.name
        )
        ts = _utc_now()
        log_fn(f"{ts} | sources-error | {query_page.relative_to(vault_root).as_posix()} (malformed ## Sources section)")
        result.malformed_sources_pages.append(query_page)
        return
```

**Step 3 — Check for existing stale banner**

Scan the full file content for a line matching the pattern `> [!warning] Stale Content`. If found, the page is already flagged:

```python
    if _has_stale_banner(content):
        logger.debug("Query page %s already has stale banner; skipping", query_page.name)
        result.already_stale_pages.append(query_page)
        return
```

**Step 4 — Check if any source is in the change-set**

```python
    stale_sources = [s for s in sources if _normalize_source(s) in changed_vault_paths]

    if not stale_sources:
        result.clean_pages.append(query_page)
        return
```

**Step 5 — Insert stale banner and annotate index**

```python
    _insert_stale_banner(query_page, stale_sources, logger)
    _annotate_index_row(vault_root, query_page, logger)

    rel_path = query_page.relative_to(vault_root).as_posix()
    changed_sources_str = ", ".join(stale_sources[:3])  # list first few for log
    ts = _utc_now()
    log_fn(f"{ts} | query-stale | {rel_path} (sources changed: {changed_sources_str})")
    logger.info("Flagged stale: %s", rel_path)
    result.flagged_pages.append(query_page)
```

---

#### `_parse_sources_section()` Internal Helper

Extracts the list of source paths from the `## Sources` section of a query page.

```python
_SOURCES_HEADING_RE = re.compile(r"^##\s+Sources\s*$", re.MULTILINE)
_SOURCE_ITEM_RE = re.compile(r"^\s*-\s+(\S+)", re.MULTILINE)


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
    paths = []
    for item_match in _SOURCE_ITEM_RE.finditer(section_text):
        path_str = item_match.group(1)
        # Strip annotation suffixes like "(too large to include)"
        # A path ends at the first whitespace
        paths.append(path_str.strip())

    return paths  # may be [] if section exists but has no list items
```

Sources with `(too large to include)` annotations are handled: `_SOURCE_ITEM_RE` captures `\S+` which stops at whitespace before the annotation, leaving just the path token.

---

#### `_normalize_source()` Internal Helper

Normalizes a source path from `## Sources` to match the format in `changed_vault_paths`. The source paths in `## Sources` are vault-relative with forward slashes (e.g. `src/auth/login.py.md`). The `changed_vault_paths` set uses the same format.

```python
def _normalize_source(source: str) -> str:
    """Normalize a source path from ## Sources to vault-relative forward-slash format."""
    # Strip leading/trailing whitespace; convert any backslashes to forward slashes
    return source.strip().replace("\\", "/")
```

---

#### `_has_stale_banner()` Internal Helper

Scans the full file content for a line matching the stale banner pattern. Per spec FR-3.8 step 3a: detection uses `> [!warning] Stale Content` as the pattern.

```python
_STALE_BANNER_RE = re.compile(r"^>\s*\[!warning\]\s*Stale Content", re.MULTILINE)


def _has_stale_banner(content: str) -> bool:
    return bool(_STALE_BANNER_RE.search(content))
```

---

#### `_insert_stale_banner()` Internal Helper

Inserts the stale callout banner immediately after the H1 title line (and any blank line that immediately follows it) in the query page file. Per FR-3.8 step 3a and AT-22: the H1 title must remain the first line of the file.

**Banner format**:
```
> [!warning] Stale Content
> The following source files changed since this answer was saved: `path/to/changed.py.md`
> Run `codewiki lint` to regenerate this answer.
```

**Insertion algorithm**:
1. Split content into lines.
2. Find the first line starting with `# ` (the H1). If none found, insert after line 0 as a fallback.
3. Skip blank lines immediately following the H1.
4. Insert the banner block at that position (before the first non-blank line after the H1).
5. Write the modified content back to the file.

```python
def _insert_stale_banner(
    query_page: Path,
    stale_sources: list[str],
    logger: logging.Logger,
) -> None:
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
```

---

#### `_annotate_index_row()` Internal Helper

Finds the query page's row in `index.md` and appends ` ⚠ stale` to its Description column. Operates by reading `index.md`, locating the row containing the query page's wikilink, replacing that row with an annotated version, and writing back.

```python
def _annotate_index_row(
    vault_root: Path,
    query_page: Path,
    logger: logging.Logger,
) -> None:
    index_path = vault_root / "index.md"
    if not index_path.exists():
        logger.warning("index.md not found; cannot annotate stale row for %s", query_page.name)
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
            # Table row format: | [[...]] | Description |
            # Strip trailing newline, append annotation, restore newline
            stripped = line.rstrip("\n")
            # Find last pipe to append before closing pipe
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
        logger.warning("Could not find index.md row for %s to annotate", query_page.name)
```

---

#### `_utc_now()` Utility

```python
from datetime import datetime, timezone

def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
```

---

## Error Handling

| Condition | Location | Behavior |
|-----------|----------|----------|
| `index.md` does not exist on first run | `_parse_existing_index()` | Returns empty dict — first-run scenario, no prior descriptions to preserve |
| Query page unreadable | `_process_query_page()` | Logged at ERROR; added to `malformed_sources_pages`; processing continues for other pages |
| `## Sources` section missing | `_parse_sources_section()` | Returns `None`; caller logs hard error to `log.md` and debug log; page added to `malformed_sources_pages`; ingest continues |
| `## Sources` section empty (no list items) | `_parse_sources_section()` | Returns `[]`; same hard-error handling as missing section |
| H1 line not found in query page | `_insert_stale_banner()` | Banner inserted at position 1 (after line 0) as fallback; logged at WARNING |
| `query_page.write_text()` fails after banner computation | `_insert_stale_banner()` | Logged at ERROR; file left unchanged; `flagged_pages` still includes this page (banner insertion attempted) |
| `index.md` row for query page not found | `_annotate_index_row()` | Logged at WARNING; index annotation skipped; stale banner was already inserted; no abort |
| `index.md` unreadable or unwritable | `_annotate_index_row()` | Logged at ERROR; annotation skipped; no abort |
| `queries/` directory does not exist | `detect_stale_queries()` | Returns empty `StalenessResult` (no-op) |

---

## Unit Test Specifications

**Files**: `tests/test_index_writer.py`, `tests/test_staleness.py`

All tests use `tmp_path`. No LLM calls. No network.

---

### `index_writer.py` — `rebuild_index()`

#### Basic table structure

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Empty vault | Vault with no summary files, no queries/ | `index.md` created with header rows only | FR-3.6: complete rebuild even if empty |
| Single summary file | `vault/src/foo.py.md` | `index.md` contains row `\| [[src/foo.py]] \| ... \|` | Summary file included |
| File at vault root | `vault/main.py.md` | Row with `[[main.py]]` | Root-level summary |
| Nested summary | `vault/src/auth/login.py.md` | Row with `[[src/auth/login.py]]` | Nested path |
| Root overview | `vault/overview.md` | Row with description "Top-level application overview" | FR-3.6: overview included |
| Subdir overview | `vault/src/auth/overview.md` | Row with description "Directory overview: src/auth/" | FR-4: subdirectory overviews |
| Query page included | `vault/queries/how-auth-works.md` | Row with `[[queries/how-auth-works]]` | FR-3.6: query pages preserved |
| Multiple page types | Mix of summaries, overview, query pages | All present in table | FR-3.6: complete table |

#### Description extraction

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Standard summary | File with H1 then blank then description paragraph | First non-blank non-heading line after H1 | `_extract_description()` logic |
| Description truncated | First prose line > 120 chars | Truncated to 120 chars | Truncation rule |
| No body after H1 | File with only H1 | `"(no description)"` | Fallback |
| Section heading after H1 | File with H1 then `## References` immediately | `"(no description)"` (heading hit before prose) | Heading terminates search |
| Unreadable file | File with no read permission | `"(no description)"` | OSError fallback |

#### Preserving existing descriptions and annotations

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Query page description preserved | `index.md` has `\| [[queries/q]] \| Describes auth flow \|`; query page exists | New index retains "Describes auth flow" | Query descriptions set by `save_query_page()` |
| Stale annotation preserved | `index.md` has `\| [[queries/q]] \| Describes auth flow ⚠ stale \|` | New index retains "⚠ stale" annotation | Stale state preserved across rebuild |
| New summary gets fresh description | Summary file new (not in old index) | Description extracted from file content | No old entry to reuse |

#### Overwrite behavior

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Completely rewrites | Old `index.md` has stale rows for deleted summaries | New `index.md` has no rows for deleted summaries | FR-3.6: complete rebuild, not append |

---

### `staleness.py` — `detect_stale_queries()`

#### No-op cases

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| No queries/ directory | Fresh vault, no queries/ | Returns empty `StalenessResult` | FR-3.8 step 6: first ingest is no-op |
| queries/ empty | queries/ exists but has no .md files | Returns empty `StalenessResult` | No pages to check |
| No changed vault paths | All sources unchanged | All pages in `clean_pages`; none flagged | No staleness |

#### Staleness detection

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Source in change-set → stale | Query page sources `src/auth/login.py.md`; that path derived in the change_set | Page in `flagged_pages`; stale banner inserted; index annotated | FR-3.8 step 3 |
| Multiple sources, one changed | Query page has 3 sources; 1 in change-set | Page flagged stale; banner lists the changed source | Any match triggers flag |
| All sources unchanged | Query page has sources not in change-set | Page in `clean_pages`; no banner | FR-3.8: only flag if source changed |
| Deleted summary triggers stale | Query page sources `src/old.py.md`; that path derived in the change_set (from deleted_summaries) | Page flagged | FR-3.8: deleted paths included in change-set |

#### Banner placement (AT-22, AT-23)

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Banner after H1 | Query page: `# Title\n\nAnswer...`; stale | Banner appears after `# Title` line | AT-22: H1 must be first line |
| H1 still first line | Query page with H1 on line 1; stale | After modification, H1 is still on line 1 | AT-22 |
| Banner after H1 with blank line | Query page: `# Title\n\nAnswer text...` | Banner after the `# Title` and blank line, before answer | FR-3.8: after H1 and any blank line |
| No duplicate banner (AT-23) | Query page already has `> [!warning] Stale Content` | Page added to `already_stale_pages`; no second banner | AT-23 |
| Existing banner survives | Page already stale; same run | File unchanged (no write); single banner | AT-23 |

**Key Scenario: Banner inserted after H1 (AT-22)**

```python
def test_stale_banner_placed_after_h1(tmp_path):
    from codebase_wiki_builder.staleness import detect_stale_queries
    import logging

    vault = tmp_path / "vault"
    vault.mkdir()
    queries_dir = vault / "queries"
    queries_dir.mkdir()

    # Create index.md
    (vault / "index.md").write_text(
        "| File | Description |\n|------|-------------|\n"
        "| [[queries/how-auth-works]] | Explains auth |\n"
    )

    # Create query page
    query_page = queries_dir / "how-auth-works.md"
    query_page.write_text(
        "# How does auth work?\n\nAuthentication uses JWT tokens.\n\n"
        "## Sources\n- src/auth/login.py.md\n\n"
        "## Page Metadata\nsaved_at: 2026-04-29 10:00:00 UTC\n"
    )

    from codebase_wiki_builder.scanner import ChangeSet
    from codebase_wiki_builder.vault import vault_path_for_source

    codebase = tmp_path / "codebase"
    codebase.mkdir()
    src_auth = codebase / "src" / "auth"
    src_auth.mkdir(parents=True)
    login_py = src_auth / "login.py"
    login_py.write_text("def login(): pass")

    change_set = ChangeSet(new_files=[login_py])
    codebase_root = codebase
    logger = logging.getLogger("test")
    log_entries = []

    result = detect_stale_queries(change_set, vault, codebase_root, log_entries.append, logger)

    assert len(result.flagged_pages) == 1
    content = query_page.read_text(encoding="utf-8")
    lines = content.splitlines()

    # H1 is still the first line
    assert lines[0] == "# How does auth work?"

    # Stale banner appears before answer body
    assert any("> [!warning] Stale Content" in line for line in lines)

    # Banner is NOT before the H1
    h1_idx = next(i for i, l in enumerate(lines) if l.startswith("# "))
    banner_idx = next(i for i, l in enumerate(lines) if "[!warning]" in l)
    assert banner_idx > h1_idx
```

**Key Scenario: No duplicate banner (AT-23)**

```python
def test_no_duplicate_stale_banner(tmp_path):
    from codebase_wiki_builder.staleness import detect_stale_queries
    import logging

    vault = tmp_path / "vault"
    vault.mkdir()
    queries_dir = vault / "queries"
    queries_dir.mkdir()

    (vault / "index.md").write_text(
        "| File | Description |\n|------|-------------|\n"
        "| [[queries/q]] | Description ⚠ stale |\n"
    )

    query_page = queries_dir / "q.md"
    # Already has a stale banner
    query_page.write_text(
        "# Question?\n\n"
        "> [!warning] Stale Content\n"
        "> The following source files changed since this answer was saved: `src/foo.py.md`\n"
        "> Run `codewiki lint` to regenerate this answer.\n\n"
        "Answer text.\n\n"
        "## Sources\n- src/foo.py.md\n"
    )

    from codebase_wiki_builder.scanner import ChangeSet

    codebase = tmp_path / "codebase"
    codebase.mkdir()
    src_foo = codebase / "src" / "foo.py"
    src_foo.parent.mkdir(parents=True, exist_ok=True)
    src_foo.write_text("def foo(): pass")

    change_set = ChangeSet(new_files=[src_foo])
    codebase_root = codebase
    logger = logging.getLogger("test")
    log_entries = []

    result = detect_stale_queries(change_set, vault, codebase_root, log_entries.append, logger)

    assert len(result.already_stale_pages) == 1
    assert len(result.flagged_pages) == 0

    content = query_page.read_text(encoding="utf-8")
    # Only one stale banner
    assert content.count("> [!warning] Stale Content") == 1
```

#### Hard error: missing/malformed Sources (AT-24)

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| No ## Sources section (AT-24) | Query page has no `## Sources` heading | `malformed_sources_pages` includes page; `sources-error` in log entry; processing continues for other pages | AT-24: hard error, not silent skip |
| Empty ## Sources section | `## Sources` heading but no list items | `malformed_sources_pages` includes page | FR-3.8 step 2: malformed = hard error |
| Other pages unaffected | 3 pages: 1 malformed, 2 normal | 2 normal pages processed; only 1 in malformed list | FR-3.8: ingest continues |

**Key Scenario: Missing Sources — hard error but no abort (AT-24)**

```python
def test_missing_sources_hard_error_continues(tmp_path):
    from codebase_wiki_builder.staleness import detect_stale_queries
    import logging

    vault = tmp_path / "vault"
    vault.mkdir()
    queries_dir = vault / "queries"
    queries_dir.mkdir()

    (vault / "index.md").write_text(
        "| File | Description |\n|------|-------------|\n"
        "| [[queries/malformed]] | Bad query |\n"
        "| [[queries/good]] | Good query |\n"
    )

    # Malformed: no ## Sources
    bad_page = queries_dir / "malformed.md"
    bad_page.write_text("# A question?\n\nSome answer.\n\n## Page Metadata\nsaved_at: ...\n")

    # Good query page
    good_page = queries_dir / "good.md"
    good_page.write_text(
        "# Good question?\n\nAnswer.\n\n## Sources\n- src/other.py.md\n"
    )

    from codebase_wiki_builder.scanner import ChangeSet

    codebase = tmp_path / "codebase"
    codebase.mkdir()
    change_set = ChangeSet()  # no changes; just testing error handling
    codebase_root = codebase
    logger = logging.getLogger("test")
    log_entries = []

    result = detect_stale_queries(change_set, vault, codebase_root, log_entries.append, logger)

    # Malformed page reported
    assert len(result.malformed_sources_pages) == 1
    assert bad_page in result.malformed_sources_pages

    # Good page still processed (clean, since no changes)
    assert len(result.clean_pages) == 1
    assert good_page in result.clean_pages

    # Error logged to log.md
    assert any("sources-error" in e for e in log_entries)
```

#### `_parse_sources_section()`

| Case | Input | Expected | Why |
|------|-------|----------|-----|
| Missing section | Content with no `## Sources` | `None` | Hard error trigger |
| Empty section | `## Sources\n\n## Next Section` | `[]` | Malformed trigger |
| Well-formed | `## Sources\n- src/auth/login.py.md\n` | `["src/auth/login.py.md"]` | Happy path |
| Multiple sources | Two `- path` items | Both paths returned | Multi-source |
| With annotation | `- src/big.py.md (too large to include)` | `["src/big.py.md"]` | Annotation stripped |
| Sources at end of file | `## Sources\n- src/foo.py.md\n` (EOF, no next `##`) | `["src/foo.py.md"]` | No next heading required |

#### Index annotation

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Annotates correct row | `index.md` has row for query page | That row gains ` ⚠ stale` in Description | FR-3.8 step 3b |
| Only annotates matching row | Multiple rows; only one is query page | Only that row annotated | No false positives |
| Already annotated row skipped | Row already has ` ⚠ stale` | Row unchanged | Idempotent |
| Missing row gracefully handled | query page not in `index.md` | WARNING logged; no crash | Defensive |

#### log.md entries

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Flagged page logged | One page flagged stale | `log_fn` called with `query-stale` entry matching spec format | FR-3.8 step 4 |
| Malformed page logged | Page with missing Sources | `log_fn` called with `sources-error` entry | AT-24 |
| Clean page not logged | Page with unchanged sources | `log_fn` not called for that page | Only log state changes |

---

## Notes

- **`rebuild_index()` is a complete rewrite, not an append**: The spec states index.md SHALL be completely rewritten on each ingest. No content from the old index.md survives except the two pieces explicitly preserved: query page descriptions and existing stale/unknowable annotations. Summary file descriptions are always re-extracted from current file content.

- **Stale annotation preservation during `rebuild_index()`**: `rebuild_index()` carries forward annotations (including ` ⚠ stale`) from the old index for existing query pages. `detect_stale_queries()` may then ADD NEW annotations. This two-step design means: (a) previously-flagged pages that are still stale keep their annotation across the rebuild without needing re-detection, and (b) newly-stale pages are flagged after the rebuild.

- **`detect_stale_queries()` accepts `ChangeSet` and `codebase_root` directly**: The function internally converts the `ChangeSet` to a `set[str]` of vault-relative path strings using `vault_path_for_source()`. The `codebase_root` parameter (derived from `config.codebase_path` by the caller) is required for this conversion. This keeps the path-conversion logic self-contained in `staleness.py` and simplifies the ingest CLI (item 9) call site — it passes the raw `ChangeSet` instead of pre-computing the vault path set.

- **`_parse_sources_section()` stops at the next `##` heading**: This correctly bounds the sources section even when `## Page Metadata` immediately follows `## Sources`. The regex `^##\s+` (multiline) detects any level-2 heading.

- **The stale banner includes a blank line before and after**: The banner block in `_insert_stale_banner()` includes surrounding blank lines to conform to Obsidian callout rendering conventions. The H1 title line itself has no blank line prepended — it stays as the first line.

- **`_collect_summary_pages()` and `_collect_overview_pages()` share exclusion constants**: `index_writer.py` imports `VAULT_SPECIAL_FILES` and `VAULT_EXCLUDED_DIRS` from `vault.py` (item 4). This is the single authoritative source for these constants — no local redefinitions in `index_writer.py`, `deletion.py`, or `scanner.py`.

- **`wikilink()` from `vault.py` is used for index table File column**: This ensures consistent formatting (forward slashes, no `.md` extension) across all index rows regardless of OS.

- **The `already_stale_pages` field in `StalenessResult`**: This exists to give the CLI accurate counts for its terminal summary (e.g., "2 pages already stale, 1 newly flagged"). The spec says ingest takes "no additional action" for already-stale pages — they are tracked but not re-processed.

- **Order of operations is critical**: `rebuild_index()` must run BEFORE `detect_stale_queries()`. `rebuild_index()` creates the table that `detect_stale_queries()` annotates. Both must run AFTER `apply_deletions()` so the index reflects the post-deletion vault state.

- **`malformed_sources_pages` are reported at the end of the ingest run**: The ingest CLI (item 9) collects `result.malformed_sources_pages` from the `StalenessResult` and reports them to the user in the terminal summary. This fulfills AT-24(c): "the affected filename is reported to the user at the end of the run."

- **`(too large to include)` sources are still checked for staleness**: A source listed as `(too large to include)` in `## Sources` is still a real file path. If that file changed, the query answer may be stale (even though the answer was generated without seeing the full file). The annotation is stripped by `_parse_sources_section()` to recover the bare path for comparison.
