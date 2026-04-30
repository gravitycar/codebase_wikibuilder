# Implementation Plan: Query Page Persistence and Slug Management

## Spec Context

This plan implements the persistence layer for saved query answers. When a user (CLI) or AI agent (MCP) saves a query result, this module writes the page to `queries/<slug>.md`, appends a row to `index.md`, and logs a `query-saved` entry to `log.md`. It also provides `read_query_page()` for downstream use by the lint command (item 14), which needs to read and rewrite query pages without losing their structure.

Catalog item: 12 — Query Page Persistence and Slug Management
Specification section: FR-5 (save prompt flow, slug logic, numeric suffix, no-overwrite rule, `## Sources` in saved page, `## Page Metadata` footer, `query-saved` log, index row with LLM-generated description), FR-3.8 (saved pages must have well-formed `## Sources` for staleness detection)
Acceptance criteria addressed: AT-13 (query answer persistence: file exists, H1 title, full answer body, `## Sources`, `## Page Metadata` with `saved_at`/`updated_at`, `index.md` row with LLM description, `log.md` `query-saved` entry)

## Dependencies

- **Blocked by**:
  - Item 4 (Vault File Utilities + Logging) — needs `slugify()`, `append_log_md()`, `wikilink()`
  - Item 8 (Index + Staleness) — needs `index.md` to exist with the expected two-column table format for row appending; needs `_parse_existing_index()` pattern knowledge for reading the current table
  - Item 11 (Query Core Logic) — needs `QueryResult` dataclass (its `answer`, `sources`, `one_line_summary` fields populate the saved page)
- **Blocks**:
  - Item 13 (Query CLI) — CLI calls `save_query_page()` after prompting the user
  - Item 14 (Lint Part 1: Staleness Resolution) — lint reads query pages via `read_query_page()`
- **Uses**: `pathlib` (stdlib), `re` (stdlib), `datetime` (stdlib), `dataclasses` (stdlib), `logging` (stdlib); `slugify()` and `append_log_md()` and `wikilink()` from `vault.py`/`logging_setup.py`; `QueryResult` from `query_engine.py`

## File Changes

### New Files

- `codebase_wiki_builder/query_persistence.py` — `save_query_page()`, `read_query_page()`, `QueryPage` dataclass, slug/deduplication helpers, page format construction, index row append, log entry

### Modified Files

- None

---

## Implementation Details

### `query_persistence.py`

**File**: `codebase_wiki_builder/query_persistence.py`

**Exports**:
- `QueryPage` — dataclass representing a parsed saved query page (for lint use)
- `save_query_page(question: str, result: QueryResult, vault_root: Path, log_fn: Callable[[str], None]) -> Path` — persist a query result; returns the path of the saved file
- `read_query_page(path: Path) -> QueryPage` — parse an existing saved query page

---

### `QueryPage` Dataclass

Used by the lint command (item 14) to read and inspect saved query pages. Contains all fields needed for staleness resolution and deduplication.

```python
from dataclasses import dataclass
from pathlib import Path


@dataclass
class QueryPage:
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
```

The `raw_content` field allows lint to manipulate the file without re-reading it. All other fields are parsed from `raw_content` for convenience.

---

### `save_query_page()` — Main Entry Point

**Signature**:

```python
def save_query_page(
    question: str,
    result: QueryResult,
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
    """
```

`log_fn` is a callable that accepts a pre-formatted log entry string and writes it to `log.md` — matching the pattern established in items 4 and 8 (`append_log_md` wrapped in a lambda or partial). This keeps `save_query_page()` decoupled from the vault root for logging purposes (the caller provides the function).

---

### Step 1 — Slug Generation

```python
from codebase_wiki_builder.vault import slugify

def _make_slug(question: str) -> str:
    """Convert question to URL-safe slug. Falls back to 'query' if result is empty."""
    slug = slugify(question)
    return slug if slug else "query"
```

`slugify()` from `vault.py` (item 4) handles the conversion: lowercase, spaces to hyphens, strip non-alphanumeric. The fallback `"query"` handles the edge case where the question consists entirely of non-alphanumeric characters (per the Notes in item 4's plan).

---

### Step 2 — Numeric-Suffix Deduplication

Per spec FR-5: if `queries/<slug>.md` already exists, append `-2`, `-3`, etc. until an unused filename is found. Never overwrite.

```python
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
```

This loop is correct for the spec's requirement: numeric suffix starts at 2 (not 1), matching the examples `how-does-auth-work-2.md`, `how-does-auth-work-3.md`.

---

### Step 3 — Build Page Content

The saved page format (per FR-5 and AT-13) is:

```
# <original question>

<full answer body — includes the ## Sources section and overflow note already embedded in result.answer>

## Page Metadata
saved_at: YYYY-MM-DD HH:MM:SS UTC
updated_at: YYYY-MM-DD HH:MM:SS UTC
```

**Important structural detail**: `result.answer` from `QueryResult` (item 11) already contains the complete formatted answer including the `## Sources` section as a trailing block. The page is assembled as:

```
H1 title
blank line
result.answer   ← contains answer body + "## Sources" section
blank line
## Page Metadata
saved_at: <timestamp>
updated_at: <timestamp>
```

```python
from datetime import datetime, timezone


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _build_page_content(question: str, result: QueryResult, timestamp: str) -> str:
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
```

Both `saved_at` and `updated_at` are set to the same timestamp at creation (per FR-5 spec: "set at creation; updated whenever the page content changes"). The lint command (item 14) updates `updated_at` but never touches `saved_at`.

---

### Step 4 — Append Row to `index.md`

The new query page row must be appended to `index.md` using the same two-column table format established by `index_writer.py`. The `File` column is an Obsidian wikilink; the `Description` column is the LLM-generated `one_line_summary` from `QueryResult`.

`save_query_page()` appends a single row rather than rebuilding the entire index. This is intentional: the full index rebuild is done by `rebuild_index()` (item 8) during ingest. During a `query` save operation, only one new row is needed.

```python
from codebase_wiki_builder.vault import wikilink


def _append_index_row(
    vault_root: Path,
    page_path: Path,
    description: str,
    logger: logging.Logger,
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
        logger.debug("Created index.md with query page row: %s", link)
        return

    try:
        existing = index_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Cannot read index.md to append query row: %s", exc)
        return

    # Append the new row at the end of the file
    updated = existing.rstrip("\n") + "\n" + new_row
    try:
        index_path.write_text(updated, encoding="utf-8")
        logger.debug("Appended index.md row for %s", link)
    except OSError as exc:
        logger.error("Cannot write updated index.md: %s", exc)
```

**Why append instead of rebuild**: During CLI `query` save, we are not in an ingest context — we only have the new page's data, not all other pages' data. Rebuilding the full index would require re-reading every page to re-extract descriptions (expensive). Appending is safe here because the ingest command fully rebuilds `index.md` on the next run anyway (item 8).

---

### Step 5 — Log Entry

Per FR-5 and FR-6.1, the `query-saved` entry format is:
```
YYYY-MM-DD HH:MM:SS UTC | query-saved | [question] → queries/[filename]
```

```python
def _write_log_entry(
    question: str,
    page_path: Path,
    vault_root: Path,
    timestamp: str,
    log_fn: Callable[[str], None],
) -> None:
    rel_path = page_path.relative_to(vault_root).as_posix()
    log_fn(f"{timestamp} | query-saved | {question} → {rel_path}")
```

---

### Complete `save_query_page()` Implementation

```python
import logging
from pathlib import Path
from typing import Callable

from codebase_wiki_builder.query_engine import QueryResult
from codebase_wiki_builder.vault import slugify, wikilink

logger = logging.getLogger(__name__)


def save_query_page(
    question: str,
    result: QueryResult,
    vault_root: Path,
    log_fn: Callable[[str], None],
) -> Path:
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
```

---

### `read_query_page()` — For Lint Use

The lint command (item 14) calls `read_query_page()` to parse an existing saved query page into a `QueryPage` dataclass. This provides structured access to the H1 title (original question), `saved_at`/`updated_at` timestamps, and `## Sources` section.

**Signature**:

```python
def read_query_page(path: Path) -> QueryPage:
    """Parse a saved query page file into a QueryPage dataclass.

    Raises:
        OSError: if the file cannot be read.
        ValueError: if the file is missing required structure (no H1 title).
    """
```

**Parsing algorithm**:

The query page file has this structure:
```
# <question>
[optional blank lines]
[optional stale/unknowable banner block(s)]
[blank line]
<answer body text>

## Sources
- src/auth/login.py.md
- ...

## Page Metadata
saved_at: YYYY-MM-DD HH:MM:SS UTC
updated_at: YYYY-MM-DD HH:MM:SS UTC
```

Parse steps:
1. Read the full file content.
2. Extract H1 title: first line starting with `# `.
3. Extract `saved_at` and `updated_at`: search for `^saved_at: ` and `^updated_at: ` in the `## Page Metadata` section.
4. Extract sources: reuse the same `_parse_sources_section()` logic as `staleness.py` (item 8) — implemented locally in this module rather than imported from `staleness.py` to avoid circular imports.
5. Extract answer body: the text between the H1 (and banners) and the first `## Sources` or `## Page Metadata` heading.

```python
# Regex patterns for parsing
_H1_RE = re.compile(r"^# (.+)$", re.MULTILINE)
_SOURCES_HEADING_RE = re.compile(r"^##\s+Sources\s*$", re.MULTILINE)
_SOURCE_ITEM_RE = re.compile(r"^\s*-\s+(\S+)", re.MULTILINE)
_PAGE_METADATA_RE = re.compile(r"^##\s+Page Metadata\s*$", re.MULTILINE)
_SAVED_AT_RE = re.compile(r"^saved_at:\s*(.+)$", re.MULTILINE)
_UPDATED_AT_RE = re.compile(r"^updated_at:\s*(.+)$", re.MULTILINE)


def read_query_page(path: Path) -> QueryPage:
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
```

**`_extract_sources(content: str) -> list[str]`** — mirrors `_parse_sources_section()` from `staleness.py`:

```python
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
```

**`_extract_field(content: str, pattern: re.Pattern, default: str) -> str`**:

```python
def _extract_field(content: str, pattern: re.Pattern, default: str) -> str:
    m = pattern.search(content)
    return m.group(1).strip() if m else default
```

**`_extract_answer_body(content: str) -> str`**:

Extracts the answer text between the H1 line and the first `##` section heading (either `## Sources` or `## Page Metadata`). Skips any stale/unknowable banner lines (callout blocks starting with `>`).

```python
def _extract_answer_body(content: str) -> str:
    """Extract the answer body text.

    Starts after the H1 line (and any callout banner blocks that follow it).
    Ends before the first ## section heading.
    Returns stripped answer text.
    """
    lines = content.splitlines()
    # Find H1 line index
    h1_idx = next((i for i, l in enumerate(lines) if l.startswith("# ")), None)
    if h1_idx is None:
        return ""

    # Find first ## section heading after H1
    section_idx = next(
        (i for i in range(h1_idx + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )

    # Slice body lines; strip leading callout blocks (> lines) and blank lines
    body_lines = lines[h1_idx + 1 : section_idx]
    # Strip leading blank lines and callout banner blocks
    start = 0
    while start < len(body_lines):
        stripped = body_lines[start].strip()
        if stripped == "" or stripped.startswith(">"):
            start += 1
        else:
            break

    return "\n".join(body_lines[start:]).strip()
```

---

### Complete Module Skeleton

```python
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from codebase_wiki_builder.query_engine import QueryResult

logger = logging.getLogger(__name__)

# ── Regex patterns ──────────────────────────────────────────────────────────
_H1_RE = re.compile(r"^# (.+)$", re.MULTILINE)
_SOURCES_HEADING_RE = re.compile(r"^##\s+Sources\s*$", re.MULTILINE)
_SOURCE_ITEM_RE = re.compile(r"^\s*-\s+(\S+)", re.MULTILINE)
_PAGE_METADATA_RE = re.compile(r"^##\s+Page Metadata\s*$", re.MULTILINE)
_SAVED_AT_RE = re.compile(r"^saved_at:\s*(.+)$", re.MULTILINE)
_UPDATED_AT_RE = re.compile(r"^updated_at:\s*(.+)$", re.MULTILINE)


# ── Public dataclass ─────────────────────────────────────────────────────────
@dataclass
class QueryPage:
    path: Path
    question: str
    answer_body: str
    sources: list[str]
    saved_at: str
    updated_at: str
    raw_content: str


# ── Public functions ─────────────────────────────────────────────────────────
def save_query_page(
    question: str,
    result: "QueryResult",
    vault_root: Path,
    log_fn: Callable[[str], None],
) -> Path: ...


def read_query_page(path: Path) -> QueryPage: ...


# ── Internal helpers ─────────────────────────────────────────────────────────
def _make_slug(question: str) -> str: ...
def _unique_query_path(queries_dir: Path, slug: str) -> Path: ...
def _utc_now() -> str: ...
def _build_page_content(question: str, result: "QueryResult", timestamp: str) -> str: ...
def _append_index_row(vault_root: Path, page_path: Path, description: str, logger: logging.Logger) -> None: ...
def _write_log_entry(question: str, page_path: Path, vault_root: Path, timestamp: str, log_fn: Callable[[str], None]) -> None: ...
def _extract_sources(content: str) -> list[str]: ...
def _extract_field(content: str, pattern: re.Pattern, default: str) -> str: ...
def _extract_answer_body(content: str) -> str: ...
```

---

## Error Handling

| Condition | Behavior |
|-----------|----------|
| `queries/` directory cannot be created | `OSError` propagates — fatal for the save operation |
| `page_path.write_text()` fails | `OSError` propagates — caller (CLI or MCP) handles as save failure |
| `index.md` cannot be read | Logged at ERROR; index row not appended; save continues (page is already written) |
| `index.md` cannot be written | Logged at ERROR; same — page written, index not updated |
| `log_fn` raises | `OSError` propagates — caller decides how to handle |
| `read_query_page()` on unreadable file | `OSError` propagates (caller must handle) |
| `read_query_page()` file has no H1 | Raises `ValueError("Query page <path> has no H1 title line")` |
| `_extract_sources()` finds no `## Sources` | Returns `[]` — lint/staleness modules treat missing sources as a hard error separately |
| `saved_at` or `updated_at` absent in metadata | Returns `""` for that field — lint deduplication falls back to `saved_at`, then row order |
| `_unique_query_path()` slug is `"query"` fallback | Functions normally; `query.md`, `query-2.md`, etc. |

---

## Unit Test Specifications

**File**: `tests/test_query_persistence.py`

All tests use `tmp_path`. LLM calls are not made (persistence module has no LLM calls). No real network calls.

---

### `_make_slug()`

| Case | Input | Expected | Why |
|------|-------|----------|-----|
| Normal question | `"How does auth work?"` | `"how-does-auth-work"` | Spec example |
| All non-alphanumeric | `"???"` | `"query"` | Fallback to "query" |
| Empty string | `""` | `"query"` | Fallback |
| Mixed case with trailing punct | `"What is JWT??"` | `"what-is-jwt"` | Lowercase, strip punct |

---

### `_unique_query_path()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| No conflict | Empty `queries/` dir | Returns `queries/slug.md` | Happy path |
| One conflict | `queries/slug.md` exists | Returns `queries/slug-2.md` | Numeric suffix |
| Two conflicts | `queries/slug.md` and `slug-2.md` exist | Returns `queries/slug-3.md` | Continues incrementing |
| Never overwrites | Existing file at returned path | Impossible — loop guarantees unused path | Spec invariant |

---

### `_build_page_content()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Basic structure | `question="How does auth work?"`, answer includes `## Sources` | Content starts with `# How does auth work?` | H1 is first line |
| Metadata footer present | Any inputs | Content contains `## Page Metadata`, `saved_at:`, `updated_at:` | AT-13(b) |
| Timestamps equal at creation | Single call | `saved_at` value == `updated_at` value | FR-5: both set at creation |
| Sources section preserved | `result.answer` includes `## Sources` | `## Sources` present in output | AT-13(b) |

---

### `save_query_page()` — happy path (AT-13)

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| File created | Mock `QueryResult` with answer, sources, one_line_summary; fresh vault | File exists at returned path | AT-13(a) |
| H1 title is question | Any question | First line of file is `# <question>` | AT-13(b) |
| Answer body in file | `result.answer = "Answer text\n\n## Sources\n- foo.md"` | File contains "Answer text" | AT-13(b) |
| Sources section in file | result has sources | `## Sources` present in file | AT-13(b) |
| Page Metadata footer in file | Any save | `## Page Metadata` section with `saved_at` and `updated_at` | AT-13(b) |
| Returns correct path | Save to fresh vault | Returned `Path` matches `queries/<slug>.md` | Caller uses this path |
| `queries/` dir created | `queries/` does not exist | Directory is created automatically | First-time save |

**Key Scenario: Complete page structure (AT-13)**

```python
def test_save_query_page_complete_structure(tmp_path):
    from unittest.mock import MagicMock
    from codebase_wiki_builder.query_persistence import save_query_page
    from codebase_wiki_builder.query_engine import QueryResult

    vault = tmp_path / "vault"
    vault.mkdir()

    # Create index.md (needed for _append_index_row)
    (vault / "index.md").write_text(
        "| File | Description |\n|------|-------------|\n"
    )

    result = QueryResult(
        answer="Auth uses JWT tokens.\n\n## Sources\n- src/auth/login.py.md",
        sources=["src/auth/login.py.md"],
        one_line_summary="Explains how authentication uses JWT tokens",
        stale_warnings=[],
    )
    log_entries = []

    saved_path = save_query_page(
        "How does auth work?",
        result,
        vault,
        log_entries.append,
    )

    assert saved_path == vault / "queries" / "how-does-auth-work.md"
    assert saved_path.exists()

    content = saved_path.read_text(encoding="utf-8")
    lines = content.splitlines()

    # H1 is the first line
    assert lines[0] == "# How does auth work?"
    # Answer body present
    assert "Auth uses JWT tokens." in content
    # Sources section present
    assert "## Sources" in content
    assert "- src/auth/login.py.md" in content
    # Page Metadata footer present
    assert "## Page Metadata" in content
    assert "saved_at:" in content
    assert "updated_at:" in content
```

---

### `save_query_page()` — index.md update (AT-13(c))

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Row added to index | `index.md` has header only | New row contains `[[queries/slug]]` and LLM description | AT-13(c) |
| Description is LLM-provided | `result.one_line_summary = "Explains auth"` | Index row description = "Explains auth" | AT-13(c): not first sentence |
| Wikilink format correct | Any save | Row contains `[[queries/how-does-auth-work]]` (no `.md`, no leading `/`) | Obsidian wikilink convention |
| Pipe in description escaped | `one_line_summary = "A \| B"` | Index row contains `A \\| B` | Table formatting safety |
| Index created if absent | No `index.md` in vault | `index.md` created with header and row | Edge case: save before first ingest |

**Key Scenario: Index row uses LLM-generated description (AT-13(c))**

```python
def test_index_row_uses_llm_description(tmp_path):
    from unittest.mock import MagicMock
    from codebase_wiki_builder.query_persistence import save_query_page
    from codebase_wiki_builder.query_engine import QueryResult

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "index.md").write_text(
        "| File | Description |\n|------|-------------|\n"
    )

    result = QueryResult(
        answer="JWT is used for auth.\n\n## Sources\n- src/auth.py.md",
        sources=["src/auth.py.md"],
        one_line_summary="Explains how the authentication middleware validates JWT tokens",
        stale_warnings=[],
    )
    log_entries = []
    save_query_page("How does auth work?", result, vault, log_entries.append)

    index_content = (vault / "index.md").read_text(encoding="utf-8")
    assert "Explains how the authentication middleware validates JWT tokens" in index_content
    assert "[[queries/how-does-auth-work]]" in index_content
```

---

### `save_query_page()` — log.md entry (AT-13(d))

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Log entry written | Save any query | `log_fn` called once | AT-13(d) |
| Log entry format | Save with question "Foo?" | Entry matches `... \| query-saved \| Foo? → queries/foo.md` | FR-6.1 format |
| Timestamp in entry | Any save | Entry starts with `YYYY-MM-DD HH:MM:SS UTC` | FR-6.1 |

```python
def test_query_saved_log_entry(tmp_path):
    from codebase_wiki_builder.query_persistence import save_query_page
    from codebase_wiki_builder.query_engine import QueryResult
    import re

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "index.md").write_text("| File | Description |\n|------|-------------|\n")

    result = QueryResult(
        answer="The answer.\n\n## Sources\n- src/foo.py.md",
        sources=["src/foo.py.md"],
        one_line_summary="Answers the question",
        stale_warnings=[],
    )
    log_entries = []
    save_query_page("What is foo?", result, vault, log_entries.append)

    assert len(log_entries) == 1
    entry = log_entries[0]
    # Matches: "YYYY-MM-DD HH:MM:SS UTC | query-saved | What is foo? → queries/what-is-foo.md"
    assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC", entry)
    assert "query-saved" in entry
    assert "What is foo?" in entry
    assert "queries/what-is-foo.md" in entry
```

---

### `save_query_page()` — deduplication (FR-5 no-overwrite)

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| No conflict | Fresh vault | `queries/slug.md` | Base case |
| One existing file | `queries/slug.md` pre-created | `queries/slug-2.md` created | Numeric suffix |
| Two existing files | `queries/slug.md` and `slug-2.md` pre-created | `queries/slug-3.md` created | Continues |
| Existing file not overwritten | `queries/slug.md` pre-created with sentinel content | Original content intact after save | FR-5 / spec constraint |

```python
def test_no_overwrite_existing_slug(tmp_path):
    from codebase_wiki_builder.query_persistence import save_query_page
    from codebase_wiki_builder.query_engine import QueryResult

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "index.md").write_text("| File | Description |\n|------|-------------|\n")

    queries_dir = vault / "queries"
    queries_dir.mkdir()
    existing = queries_dir / "how-does-auth-work.md"
    existing.write_text("ORIGINAL CONTENT")

    result = QueryResult(
        answer="New answer.\n\n## Sources\n- src/auth.py.md",
        sources=["src/auth.py.md"],
        one_line_summary="Explains auth",
        stale_warnings=[],
    )
    saved = save_query_page("How does auth work?", result, vault, lambda e: None)

    assert saved == vault / "queries" / "how-does-auth-work-2.md"
    # Original not overwritten
    assert existing.read_text(encoding="utf-8") == "ORIGINAL CONTENT"
```

---

### `read_query_page()` — happy path

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Standard page | Full well-formed query page | Returns `QueryPage` with all fields populated | Happy path |
| `question` extracted | H1 is `# How does auth work?` | `page.question == "How does auth work?"` | H1 extraction |
| `sources` extracted | `## Sources\n- src/auth.py.md` | `page.sources == ["src/auth.py.md"]` | Sources parsing |
| `saved_at` extracted | Metadata has `saved_at: 2026-04-29 10:00:00 UTC` | `page.saved_at == "2026-04-29 10:00:00 UTC"` | Timestamp parsing |
| `updated_at` extracted | Metadata has `updated_at: 2026-04-29 12:00:00 UTC` | `page.updated_at == "2026-04-29 12:00:00 UTC"` | Timestamp parsing |
| `raw_content` preserved | Any page | `page.raw_content == path.read_text()` | Lint needs full content |
| Page with stale banner | Page has `> [!warning] Stale Content` block after H1 | `page.question` still correct; banner excluded from `answer_body` | Banner-tolerant parsing |
| Sources with annotation | `- src/big.py.md (too large to include)` | `page.sources == ["src/big.py.md"]` | Annotation stripped |

**Key Scenario: Parse page with stale banner**

```python
def test_read_query_page_with_stale_banner(tmp_path):
    from codebase_wiki_builder.query_persistence import read_query_page

    page = tmp_path / "how-auth-works.md"
    page.write_text(
        "# How does auth work?\n\n"
        "> [!warning] Stale Content\n"
        "> The following source files changed: `src/auth/login.py.md`\n"
        "> Run `codewiki lint` to regenerate this answer.\n\n"
        "Authentication uses JWT tokens stored in cookies.\n\n"
        "## Sources\n"
        "- src/auth/login.py.md\n\n"
        "## Page Metadata\n"
        "saved_at: 2026-04-29 10:00:00 UTC\n"
        "updated_at: 2026-04-29 10:00:00 UTC\n"
    )

    qp = read_query_page(page)

    assert qp.question == "How does auth work?"
    assert qp.saved_at == "2026-04-29 10:00:00 UTC"
    assert qp.updated_at == "2026-04-29 10:00:00 UTC"
    assert qp.sources == ["src/auth/login.py.md"]
    # Answer body excludes the callout banner lines
    assert "> [!warning]" not in qp.answer_body
    assert "Authentication uses JWT tokens" in qp.answer_body
```

---

### `read_query_page()` — error cases

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| File not found | Path does not exist | `OSError` raised | Caller must handle |
| No H1 line | File with no `# ` prefix line | `ValueError` raised | Structural requirement |
| No `## Sources` | Page without sources section | `page.sources == []` | Tolerant: lint detects missing sources separately |
| No `## Page Metadata` | Page without metadata section | `page.saved_at == ""`, `page.updated_at == ""` | Tolerant: lint falls back gracefully |

---

### Round-trip: `save_query_page()` → `read_query_page()`

```python
def test_save_and_read_roundtrip(tmp_path):
    from codebase_wiki_builder.query_persistence import save_query_page, read_query_page
    from codebase_wiki_builder.query_engine import QueryResult

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "index.md").write_text("| File | Description |\n|------|-------------|\n")

    result = QueryResult(
        answer="The codebase uses Flask.\n\n## Sources\n- src/app.py.md\n- src/routes.py.md",
        sources=["src/app.py.md", "src/routes.py.md"],
        one_line_summary="Describes the Flask-based application architecture",
        stale_warnings=[],
    )
    saved_path = save_query_page("What patterns does this codebase use?", result, vault, lambda e: None)

    qp = read_query_page(saved_path)

    assert qp.question == "What patterns does this codebase use?"
    assert qp.sources == ["src/app.py.md", "src/routes.py.md"]
    assert "Flask" in qp.answer_body
    assert qp.saved_at != ""
    assert qp.updated_at != ""
    assert qp.saved_at == qp.updated_at  # both set at creation
    assert qp.raw_content == saved_path.read_text(encoding="utf-8")
```

---

## Notes

- **`result.answer` already contains `## Sources`**: The `QueryResult.answer` field produced by `query_engine.py` (item 11) is the complete formatted answer including the `## Sources` trailing section. `save_query_page()` does NOT re-add `## Sources` — it uses `result.answer` verbatim and appends only `## Page Metadata`. This ensures the sources section is always in the same format that `staleness.py` and `read_query_page()` expect.

- **`_extract_sources()` is a local copy, not imported from `staleness.py`**: Both `staleness.py` and `query_persistence.py` need to parse `## Sources` sections. Importing from `staleness.py` would create a coupling that could lead to import cycles (staleness depends on scanner, scanner may depend on vault, etc.). The logic is simple enough to duplicate safely. If the format ever changes, both modules need updating — but they are in the same codebase and the duplication is minimal.

- **`_append_index_row()` appends, not rebuilds**: During a `query` save operation (both CLI and MCP), only one new row is added to `index.md`. Full rebuilds happen only during `ingest` (item 8). This is safe because the next `ingest` run completely rebuilds `index.md` anyway, carrying forward all query page descriptions from the old index.

- **Lint's `updated_at` updates are NOT handled here**: The lint command (item 14) reads `QueryPage.raw_content` and writes the modified file directly, updating `updated_at` in place. This module is only responsible for the initial creation with equal `saved_at`/`updated_at` timestamps.

- **The `log_fn` parameter pattern**: Callers construct the `log_fn` like:
  ```python
  from codebase_wiki_builder.logging_setup import append_log_md
  from functools import partial
  log_fn = partial(append_log_md, vault_root)
  ```
  Or as a lambda:
  ```python
  log_fn = lambda entry: append_log_md(vault_root, entry)
  ```
  This keeps `save_query_page()` free of a hard dependency on `vault_root` for logging (it uses `vault_root` for path operations but not for constructing the log function — that's the caller's responsibility).

- **MCP server always calls `save_query_page()`**: The MCP server (item 15) calls `save_query_page()` unconditionally (no save prompt). The CLI (item 13) calls it only after the user answers `y` at the prompt. Both callers use the same function — no behavioral difference inside `save_query_page()` itself.

- **`QueryPage.path` is absolute**: The `path` field holds the absolute filesystem path of the query page file. Lint and dedup code uses `path.relative_to(vault_root)` when they need vault-relative paths for display or index operations.

- **Empty `queries/` directory guard**: `save_query_page()` creates `queries/` with `mkdir(parents=True, exist_ok=True)` — idempotent and safe on every call. The read path (`read_query_page()`) never creates directories.

- **Slug edge case — all-punctuation question**: If the user asks `"???"`, `slugify("???")` returns `""`, and `_make_slug()` falls back to `"query"`. The deduplication loop then finds `queries/query.md`, `queries/query-2.md`, etc. This is correct behavior — the page is saved, just with a generic filename.
