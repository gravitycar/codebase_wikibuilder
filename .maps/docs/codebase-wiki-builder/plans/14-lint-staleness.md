# Implementation Plan: Lint Part 1 — Staleness Resolution

## Spec Context

This plan implements the first part of the `lint` command: resolving stale query pages. When `codewiki ingest` flags query pages as stale (by inserting a `> [!warning] Stale Content` banner and annotating `index.md` rows with ` ⚠ stale`), lint Part 1 re-runs the full query workflow for each stale page, overwrites it with the fresh answer, and cleans up all stale annotations. Pages where no relevant files can be found are flagged as "unknowable" rather than regenerated.

Catalog item: 14 — Lint Part 1: Staleness Resolution
Specification section: FR-8.1 (all sub-requirements), FR-6.1 (`lint-query`, `lint-resolved`, `lint-unknowable` log entries)
Acceptance criteria addressed: AT-14 (lint staleness resolution), AT-15 (lint unknowable page)

## Dependencies

- **Blocked by**:
  - Item 9 (Ingest CLI) — `cli.py` with the Typer `app` must exist; lint CLI (item 17) will add the `lint` subcommand to it
  - Item 11 (Query Core Logic) — needs `run_query()`, `QueryResult`, `QUERY_CONTEXT_WINDOW`
  - Item 12 (Query Page Persistence) — needs `read_query_page()`, `QueryPage` dataclass; also needs the saved page format established here to be consistent with what persistence writes
- **Blocks**: Item 16 (Lint Part 2 + Part 3) — item 16 runs after Part 1 completes
- **Uses**: `pathlib`, `re`, `datetime`, `logging` (all stdlib); `QueryPage` from `query_persistence.py`; `run_query()` from `query_engine.py`; `append_log_md()` from `logging_setup.py`

## File Changes

### New Files

- `codebase_wiki_builder/lint_staleness.py` — `LintStalenessResult` dataclass, `resolve_stale_pages()`, all banner-stripping, page-rewriting, and index-annotation helpers

### Modified Files

- None

---

## Implementation Details

### `LintStalenessResult` Dataclass

**File**: `codebase_wiki_builder/lint_staleness.py`

```python
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LintStalenessResult:
    resolved_pages: list[Path]
    """Pages successfully regenerated with fresh answers."""

    unknowable_pages: list[Path]
    """Pages where re-run returned zero relevant files."""

    skipped_pages: list[Path]
    """Pages that could not be processed due to read/write errors (not aborted)."""
```

`resolved_pages` and `unknowable_pages` are mutually exclusive. `skipped_pages` covers unexpected I/O failures (the spec says "never abort on unknowable" but does not require continuing after unexpected I/O errors; treating them as skipped rather than fatal is the safer choice).

---

### `resolve_stale_pages()` — Main Entry Point

**Signature**:

```python
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
         f. Log lint-resolved.
         g. Print per-page terminal output.
      4. Print staleness resolved summary.
      5. Return LintStalenessResult.
    """
```

`log_fn` follows the same pattern as all other modules: a callable that accepts a pre-formatted log entry string and appends it to `log.md`. It is constructed by the lint CLI (item 17) as `partial(append_log_md, vault_root)`.

---

### Step 1 — Collect Stale Pages from `index.md`

```python
_STALE_ROW_RE = re.compile(r"\[\[([^\]]+)\]\].*⚠ stale")


def _collect_stale_pages(vault_root: Path) -> list[Path]:
    """Return absolute paths of stale query pages from index.md, in row order.

    Returns an empty list if index.md does not exist or has no stale rows.
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
```

This regex matches the same pattern used by `query_engine.py`'s `_collect_stale_warnings()`, but returns absolute `Path` objects rather than strings (lint needs to read and rewrite the files).

---

### Step 2 — Strip ALL Stale Banners

Per FR-8.1(a): before re-running the query, remove ALL stale banners from the file. A stale banner is a contiguous block beginning with a line matching `> [!warning] Stale Content` and continuing through all immediately following lines that begin with `>`.

```python
_STALE_BANNER_START_RE = re.compile(r"^> \[!warning\] Stale Content\s*$")


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
            # Also consume one trailing blank line after the block (if present)
            if i < len(lines) and lines[i].strip() == "":
                i += 1
            # Remove the blank line we may have already appended before the block
            if result and result[-1].strip() == "":
                result.pop()
        else:
            result.append(lines[i])
            i += 1

    return "".join(result)
```

**Why remove a trailing blank line after the block**: The banner is inserted after the H1 line with a trailing blank line. When stripping it, the blank line must also go to avoid double-blank-line artifacts in the regenerated page. The check also removes a preceding blank line from `result` to handle the blank line between the H1 and the banner.

---

### Step 3 — Internal Query Re-Run (lint-query)

The re-run uses `run_query()` from `query_engine.py` exactly as the CLI `query` command does, with two differences:
1. The log entry is `lint-query` (not the standard `query` entry).
2. The save-prompt step is skipped (the page is always overwritten automatically).

```python
from codebase_wiki_builder.query_engine import run_query, QueryResult
```

**Calling `run_query()` inside lint**:

`run_query()` may raise `typer.Exit(code=3)` when no relevant files are found. Lint catches this to enter the unknowable branch rather than letting it propagate:

```python
import typer

def _run_internal_query(
    question: str,
    vault_root: Path,
    llm_client: "LLMClient",
    config: "WikiConfig",
) -> "QueryResult | None":
    """Run the query workflow for lint. Returns None if zero relevant files found."""
    try:
        return run_query(question, vault_root, llm_client, config)
    except typer.Exit as exc:
        if exc.exit_code == 3:
            return None   # unknowable: zero relevant files
        raise            # re-raise any other Exit (e.g. code=1 for index missing)
```

`run_query()` also raises `typer.Exit(code=1)` if `index.md` is missing. That propagates up — the lint CLI (item 17) has already checked for `index.md` existence before calling `resolve_stale_pages()`, so this should not occur in practice. Re-raising keeps behavior correct if it does.

**Logging the re-run as `lint-query`**:

```python
from datetime import datetime, timezone

def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _log_lint_query(page_path: Path, vault_root: Path, log_fn: Callable[[str], None]) -> None:
    ts = _utc_now()
    rel = page_path.relative_to(vault_root).as_posix()
    log_fn(f"{ts} | lint-query | {rel} (re-run for staleness resolution)")
```

This log entry is written BEFORE calling `run_query()` so it appears in `log.md` even if `run_query()` raises. The standard per-query log entry (from FR-5) is NOT written — `run_query()` itself does not write to `log.md` (per the item 11 plan Notes section: "No `log.md` writes in `run_query()`").

---

### Step 4a — Handle Unknowable Case

When `_run_internal_query()` returns `None` (zero relevant files), the page is flagged as unknowable per FR-8.1(d):

```python
_UNKNOWABLE_BANNER = """> [!error] Unknowable
> This question cannot be answered by the current wiki or codebase.
> Run `codewiki ingest` then `codewiki lint` if the codebase has changed."""
```

**Rewrite the page for unknowable case**:

```python
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
```

Key points:
- The `# H1` title is always the first line.
- The unknowable banner is placed immediately after the H1 (per FR-8.1(d) and AT-15(b)).
- The canonical answer text `"this question cannot be answered by the wiki or the codebase"` replaces the answer body.
- The `## Sources` section is preserved from the original page (so future staleness detection still works).
- `saved_at` is preserved; `updated_at` is updated.

---

### Step 4b — Handle Resolved Case

When `_run_internal_query()` returns a `QueryResult`, the page is overwritten with the fresh answer:

```python
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
```

The `result.answer` from `QueryResult` already contains the full formatted answer including the `## Sources` section (per item 11 plan: "The `answer` field contains the complete formatted response"). No stale banner is included — the page is clean.

---

### Step 5 — Update `index.md` Annotation

After overwriting the page (either resolved or unknowable), the `index.md` row must be updated:

- For resolved pages: remove ` ⚠ stale` from the Description column; replace the description with the fresh LLM-generated `one_line_summary`.
- For unknowable pages: replace ` ⚠ stale` with ` ⊘ unknowable` in the Description column.

```python
def _update_index_row(
    vault_root: Path,
    page_path: Path,
    new_description: str | None,
    unknowable: bool,
    logger: logging.Logger,
) -> None:
    """Update the index.md row for a resolved or unknowable page.

    Args:
        new_description: Fresh one-line summary for resolved pages. None for unknowable.
        unknowable: If True, replaces ⚠ stale with ⊘ unknowable.
                    If False, removes ⚠ stale and updates description.
    """
    index_path = vault_root / "index.md"
    try:
        content = index_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Cannot read index.md to update annotation: %s", exc)
        return

    # Build the wikilink target (vault-relative, no .md extension)
    rel = page_path.relative_to(vault_root)
    # Wikilink format: [[queries/slug]] (no .md, no leading slash)
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
        logger.error("Cannot write updated index.md: %s", exc)
```

**`_replace_description_in_row(line: str, new_description: str) -> str`**:

The index row format is `| [[wikilink]] | Description |`. The description is the second cell. Replace it with the fresh summary.

```python
_INDEX_ROW_RE = re.compile(r"^(\|\s*\[\[[^\]]+\]\]\s*\|\s*)(.+?)(\s*\|)\s*$")


def _replace_description_in_row(line: str, new_description: str) -> str:
    """Replace the description cell in a two-column index.md table row."""
    m = _INDEX_ROW_RE.match(line)
    if m:
        safe_desc = new_description.replace("|", "\\|")
        return f"{m.group(1)}{safe_desc}{m.group(3)}"
    # If row doesn't match expected format, leave it unchanged
    return line
```

---

### Step 6 — Logging

```python
def _log_lint_resolved(
    page_path: Path, vault_root: Path, log_fn: Callable[[str], None]
) -> None:
    ts = _utc_now()
    rel = page_path.relative_to(vault_root).as_posix()
    log_fn(f"{ts} | lint-resolved | {rel}")


def _log_lint_unknowable(
    page_path: Path, vault_root: Path, log_fn: Callable[[str], None]
) -> None:
    ts = _utc_now()
    rel = page_path.relative_to(vault_root).as_posix()
    log_fn(f"{ts} | lint-unknowable | {rel}")
```

---

### Step 7 — Terminal Output

Per FR-8.1:
- Unknowable page: `⊘ Unknowable: queries/how-does-auth-work.md`
- Resolved page: `✓ Regenerated: queries/how-does-auth-work.md`
- Final summary: `"Staleness resolved: N pages updated."` (N = resolved count only, not unknowable)

```python
from rich.console import Console

_console = Console()

def _print_resolved(page_path: Path, vault_root: Path) -> None:
    rel = page_path.relative_to(vault_root).as_posix()
    _console.print(f"[green]✓ Regenerated:[/green] {rel}")


def _print_unknowable(page_path: Path, vault_root: Path) -> None:
    rel = page_path.relative_to(vault_root).as_posix()
    _console.print(f"[dim]⊘ Unknowable:[/dim] {rel}")
```

---

### Complete `resolve_stale_pages()` Body

```python
def resolve_stale_pages(
    vault_root: Path,
    llm_client: "LLMClient",
    config: "WikiConfig",
    log_fn: Callable[[str], None],
) -> LintStalenessResult:
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
            logger.warning("Stale page listed in index.md not found on disk: %s", page_path)
            skipped.append(page_path)
            continue

        try:
            page = read_query_page(page_path)
        except (OSError, ValueError) as exc:
            logger.error("Cannot read stale page %s: %s", page_path, exc)
            skipped.append(page_path)
            continue

        # Step a: Strip all stale banners
        cleaned_content = _strip_stale_banners(page.raw_content)
        # Re-read question from cleaned content (H1 always survives stripping)
        question = page.question

        # Step b: Log lint-query entry, then re-run query
        _log_lint_query(page_path, vault_root, log_fn)
        result = _run_internal_query(question, vault_root, llm_client, config)

        timestamp = _utc_now()

        if result is None:
            # Step c: Unknowable case
            new_content = _build_unknowable_page(page, timestamp)
            try:
                page_path.write_text(new_content, encoding="utf-8")
            except OSError as exc:
                logger.error("Cannot write unknowable page %s: %s", page_path, exc)
                skipped.append(page_path)
                continue

            _update_index_row(vault_root, page_path, None, unknowable=True, logger=logger)
            _log_lint_unknowable(page_path, vault_root, log_fn)
            _print_unknowable(page_path, vault_root)
            unknowable.append(page_path)

        else:
            # Steps d-g: Resolved case
            new_content = _build_resolved_page(question, result, page, timestamp)
            try:
                page_path.write_text(new_content, encoding="utf-8")
            except OSError as exc:
                logger.error("Cannot write resolved page %s: %s", page_path, exc)
                skipped.append(page_path)
                continue

            _update_index_row(
                vault_root,
                page_path,
                result.one_line_summary,
                unknowable=False,
                logger=logger,
            )
            _log_lint_resolved(page_path, vault_root, log_fn)
            _print_resolved(page_path, vault_root)
            resolved.append(page_path)

    # Final summary
    _console.print(f"Staleness resolved: {len(resolved)} page(s) updated.")

    return LintStalenessResult(
        resolved_pages=resolved,
        unknowable_pages=unknowable,
        skipped_pages=skipped,
    )
```

---

### Complete Module Skeleton

```python
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

_STALE_BANNER_START_RE = re.compile(r"^> \[!warning\] Stale Content\s*$")
_STALE_ROW_RE = re.compile(r"\[\[([^\]]+)\]\].*⚠ stale")
_INDEX_ROW_RE = re.compile(r"^(\|\s*\[\[[^\]]+\]\]\s*\|\s*)(.+?)(\s*\|)\s*$")

_UNKNOWABLE_BANNER = """> [!error] Unknowable
> This question cannot be answered by the current wiki or codebase.
> Run `codewiki ingest` then `codewiki lint` if the codebase has changed."""


@dataclass
class LintStalenessResult:
    resolved_pages: list[Path] = field(default_factory=list)
    unknowable_pages: list[Path] = field(default_factory=list)
    skipped_pages: list[Path] = field(default_factory=list)


def resolve_stale_pages(
    vault_root: Path,
    llm_client: "LLMClient",
    config: "WikiConfig",
    log_fn: Callable[[str], None],
) -> LintStalenessResult: ...


# Internal helpers
def _collect_stale_pages(vault_root: Path) -> list[Path]: ...
def _strip_stale_banners(content: str) -> str: ...
def _run_internal_query(
    question: str,
    vault_root: Path,
    llm_client: "LLMClient",
    config: "WikiConfig",
) -> "QueryResult | None": ...
def _build_unknowable_page(page: "QueryPage", timestamp: str) -> str: ...
def _build_resolved_page(
    question: str,
    result: "QueryResult",
    page: "QueryPage",
    timestamp: str,
) -> str: ...
def _update_index_row(
    vault_root: Path,
    page_path: Path,
    new_description: str | None,
    unknowable: bool,
    logger: logging.Logger,
) -> None: ...
def _replace_description_in_row(line: str, new_description: str) -> str: ...
def _log_lint_query(page_path: Path, vault_root: Path, log_fn: Callable[[str], None]) -> None: ...
def _log_lint_resolved(page_path: Path, vault_root: Path, log_fn: Callable[[str], None]) -> None: ...
def _log_lint_unknowable(page_path: Path, vault_root: Path, log_fn: Callable[[str], None]) -> None: ...
def _utc_now() -> str: ...
def _print_resolved(page_path: Path, vault_root: Path) -> None: ...
def _print_unknowable(page_path: Path, vault_root: Path) -> None: ...
```

---

## Error Handling

| Condition | Behavior |
|-----------|----------|
| `index.md` missing | `_collect_stale_pages()` returns `[]`; function prints "No stale query pages found." and returns empty result |
| Stale page listed in index.md not found on disk | Logged at WARNING; page added to `skipped_pages`; processing continues |
| `read_query_page()` raises `OSError` or `ValueError` | Logged at ERROR; page added to `skipped_pages`; processing continues |
| `run_query()` returns `typer.Exit(code=3)` | Caught; `result = None`; unknowable branch entered |
| `run_query()` returns `typer.Exit(code=1)` | Re-raised; propagates to lint CLI (index missing — should not occur) |
| `run_query()` raises `LLMError` | Propagates to lint CLI; lint aborts (fatal API error) |
| `page_path.write_text()` fails | Logged at ERROR; page added to `skipped_pages`; processing continues |
| `index.md` cannot be read for annotation update | Logged at ERROR; annotation skipped; page write already completed |
| `index.md` cannot be written after annotation | Logged at ERROR; page write already completed; index may be inconsistent until next ingest |
| All pages unknowable (no resolved pages) | Normal: `resolved_pages=[]`; prints "Staleness resolved: 0 page(s) updated." |

---

## Unit Test Specifications

**File**: `tests/test_lint_staleness.py`

All tests use `tmp_path`. LLM calls mocked via `unittest.mock`. No real network calls.

---

### `_strip_stale_banners()`

| Case | Input | Expected | Why |
|------|-------|----------|-----|
| No banner | Content without any `> [!warning]` | Content unchanged | Clean page |
| Single banner | Content with one stale banner block | Banner removed; H1 preserved as first line | AT-14(a) |
| Double banner | Content with two stale banner blocks | Both removed | Edge case: duplicate banners |
| Banner with extra `>` lines | Multi-line callout block | All `>` lines removed | Spec: all immediately following `>` lines |
| Banner at H1 boundary | H1 line then banner then content | H1 preserved; banner removed; content follows | AT-14(b): H1 still first line after stripping |
| Trailing blank line after banner | Banner followed by blank line then answer | Blank line consumed; no double-blank artifact | Clean output |

**Key Scenario: Double-banner cleanup**

```python
def test_strip_stale_banners_removes_all(tmp_path):
    from codebase_wiki_builder.lint_staleness import _strip_stale_banners

    content = (
        "# How does auth work?\n\n"
        "> [!warning] Stale Content\n"
        "> Sources changed.\n"
        "> Run `codewiki lint`.\n\n"
        "> [!warning] Stale Content\n"
        "> Sources changed again.\n\n"
        "Authentication uses JWT tokens.\n\n"
        "## Sources\n"
        "- src/auth.py.md\n"
    )

    result = _strip_stale_banners(content)

    assert "> [!warning] Stale Content" not in result
    assert result.startswith("# How does auth work?")
    assert "Authentication uses JWT tokens." in result
```

---

### `_collect_stale_pages()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| No stale rows | `index.md` with no ` ⚠ stale` | Returns `[]` | No stale pages |
| One stale row | Row with `⚠ stale` | Returns `[vault_root / "queries/page.md"]` | Happy path |
| Multiple stale rows | Two stale rows | Returns both paths in row order | Multiple stale pages |
| Non-query stale row | Summary file row with `⚠ stale` | Path included (staleness can apply to any page) | Defensive |
| `index.md` missing | No `index.md` in vault | Returns `[]` | No index = no stale pages |

---

### `_run_internal_query()` — catching `typer.Exit`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| `run_query()` succeeds | Mock returns `QueryResult` | Returns `QueryResult` | Happy path |
| `run_query()` exits code 3 | Mock raises `typer.Exit(code=3)` | Returns `None` | Unknowable detection |
| `run_query()` exits code 1 | Mock raises `typer.Exit(code=1)` | Re-raises `typer.Exit(code=1)` | Fatal error propagation |

---

### `_build_unknowable_page()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Structure correct | Any `QueryPage` + timestamp | H1 first; unknowable banner after H1; canonical answer text; sources preserved | AT-15(a)(b) |
| `saved_at` preserved | Page has `saved_at: 2026-01-01...` | Output has same `saved_at` | AT-14(g): saved_at unchanged |
| `updated_at` updated | Timestamp = "2026-04-30 12:00:00 UTC" | Output has new `updated_at` | AT-14(f), AT-15(f) |
| Sources preserved | Page has sources list | `## Sources` with original sources in output | Future staleness detection |
| Canonical answer text | Any input | Answer body = "this question cannot be answered by the wiki or the codebase" | AT-15(a) |

---

### `_build_resolved_page()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Structure correct | `QueryResult` + `QueryPage` + timestamp | H1 first; no stale banner; fresh answer; `## Sources`; `## Page Metadata` | AT-14(a)(b) |
| `saved_at` preserved | Page has `saved_at: 2026-01-01...` | Output has same `saved_at` | AT-14(g) |
| `updated_at` updated | New timestamp | Output has new `updated_at` | AT-14(f) |
| No stale banner in output | Any input | `> [!warning] Stale Content` not in output | AT-14(a) |
| Fresh answer used | `result.answer = "New answer."` | "New answer." in output | AT-14(a) |

---

### `_update_index_row()` — resolved case

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Removes ` ⚠ stale` | Row with ` ⚠ stale` | Row no longer contains ` ⚠ stale` | AT-14(c) |
| Updates description | Row with old description; fresh one-line summary provided | Description cell updated to new summary | AT-14(c) |
| Other rows unchanged | Two rows; only one is stale | Only matching row modified | Surgical update |
| Escaped pipe in description | New description contains `|` | Written as `\\|` | Table formatting safety |
| `index.md` missing | No index | Logs error; returns without crashing | Graceful degradation |

---

### `_update_index_row()` — unknowable case

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Replaces ` ⚠ stale` with ` ⊘ unknowable` | Row with ` ⚠ stale` | Row contains ` ⊘ unknowable`, not ` ⚠ stale` | AT-15(c) |
| Description NOT updated | Row with old description | Description unchanged (only annotation replaced) | Unknowable pages keep old description |

---

### `resolve_stale_pages()` — no stale pages

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Empty index | `index.md` with no stale rows | Prints "No stale query pages found."; returns empty result | FR-8.1 step 2 |
| No index | No `index.md` | Same: empty result, no crash | Graceful |

---

### `resolve_stale_pages()` — resolved page (AT-14)

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Page overwritten with fresh answer | Stale page; mock `run_query` returns `QueryResult` | File rewritten with H1 + answer + no stale banner | AT-14(a) |
| H1 is still first line | Any stale page | First line of rewritten file = `# <question>` | AT-14(b) |
| Index annotation removed | `index.md` row has ` ⚠ stale` | Row no longer has ` ⚠ stale` after lint | AT-14(c) |
| `lint-query` log entry written | Run for stale page | `log_fn` called with entry containing `lint-query` and filename | AT-14(d) |
| `lint-resolved` log entry written | Successful re-run | `log_fn` called with entry containing `lint-resolved` and filename | AT-14(d) |
| No standard query log entry | Run for stale page | `log_fn` NOT called with `query-saved` | AT-14(d) |
| Terminal prints `✓ Regenerated:` | Successful run | Output contains `✓ Regenerated: queries/how-does-auth-work.md` | AT-14(e) |
| `updated_at` updated | Page has old `updated_at` | Rewritten page has current timestamp | AT-14(f) |
| `saved_at` unchanged | Page has `saved_at: 2026-01-01 10:00:00 UTC` | Rewritten page has same `saved_at` | AT-14(g) |

**Key Scenario: Full resolved path (AT-14)**

```python
def test_resolve_stale_page_full_flow(tmp_path, monkeypatch):
    import json
    from unittest.mock import MagicMock, patch
    from codebase_wiki_builder.lint_staleness import resolve_stale_pages
    from codebase_wiki_builder.query_engine import QueryResult
    from codebase_wiki_builder.query_persistence import QueryPage

    vault = tmp_path / "vault"
    vault.mkdir()
    queries_dir = vault / "queries"
    queries_dir.mkdir()

    # Create a stale query page
    page_path = queries_dir / "how-does-auth-work.md"
    page_path.write_text(
        "# How does auth work?\n\n"
        "> [!warning] Stale Content\n"
        "> src/auth.py.md changed.\n"
        "> Run `codewiki lint`.\n\n"
        "Old answer about auth.\n\n"
        "## Sources\n"
        "- src/auth/login.py.md\n\n"
        "## Page Metadata\n"
        "saved_at: 2026-04-29 10:00:00 UTC\n"
        "updated_at: 2026-04-29 10:00:00 UTC\n"
    )

    # Create index.md with stale annotation
    (vault / "index.md").write_text(
        "| File | Description |\n"
        "|------|-------------|\n"
        "| [[queries/how-does-auth-work]] | Explains auth ⚠ stale |\n"
    )

    fresh_result = QueryResult(
        answer="Auth uses JWT tokens.\n\n## Sources\n- src/auth/login.py.md",
        sources=["src/auth/login.py.md"],
        one_line_summary="Explains how JWT authentication works",
        stale_warnings=[],
    )

    log_entries = []
    llm_client = MagicMock()
    config = MagicMock()

    with patch(
        "codebase_wiki_builder.lint_staleness.run_query",
        return_value=fresh_result,
    ):
        lint_result = resolve_stale_pages(vault, llm_client, config, log_entries.append)

    # (a) Page overwritten with no stale banner
    content = page_path.read_text(encoding="utf-8")
    assert "> [!warning] Stale Content" not in content
    # (b) H1 is still first line
    assert content.splitlines()[0] == "# How does auth work?"
    # (c) Index annotation removed
    index_content = (vault / "index.md").read_text(encoding="utf-8")
    assert "⚠ stale" not in index_content
    # (d) lint-query and lint-resolved in log; no query-saved
    log_text = "\n".join(log_entries)
    assert "lint-query" in log_text
    assert "lint-resolved" in log_text
    assert "query-saved" not in log_text
    # (f) updated_at changed from original
    assert "saved_at: 2026-04-29 10:00:00 UTC" in content
    assert "updated_at: 2026-04-29 10:00:00 UTC" not in content  # updated to current time
    # (g) saved_at unchanged
    assert "saved_at: 2026-04-29 10:00:00 UTC" in content
    # Result tracking
    assert len(lint_result.resolved_pages) == 1
    assert len(lint_result.unknowable_pages) == 0
```

---

### `resolve_stale_pages()` — unknowable page (AT-15)

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Unknowable banner after H1 | Mock `run_query` exits code 3 | Rewritten page has `> [!error] Unknowable` after H1 | AT-15(b) |
| Canonical answer text | Same | Page body = "this question cannot be answered by the wiki or the codebase" | AT-15(a) |
| Index shows ` ⊘ unknowable` | `index.md` has ` ⚠ stale` | After lint: ` ⊘ unknowable` (not ` ⚠ stale`) | AT-15(c) |
| `lint-unknowable` log entry | Mock exits code 3 | `log_fn` called with entry containing `lint-unknowable` | AT-15(d) |
| Lint continues | Two stale pages; first unknowable | Second page still processed | AT-15(e) |
| Terminal prints `⊘ Unknowable:` | Unknowable run | Output contains `⊘ Unknowable: queries/...` | AT-15 terminal |

**Key Scenario: Unknowable page (AT-15)**

```python
def test_resolve_unknowable_page(tmp_path, monkeypatch):
    import typer
    from unittest.mock import patch
    from codebase_wiki_builder.lint_staleness import resolve_stale_pages

    vault = tmp_path / "vault"
    vault.mkdir()
    queries_dir = vault / "queries"
    queries_dir.mkdir()

    page_path = queries_dir / "how-does-feature-x-work.md"
    page_path.write_text(
        "# How does feature X work?\n\n"
        "> [!warning] Stale Content\n"
        "> Sources changed.\n\n"
        "Old answer about feature X.\n\n"
        "## Sources\n"
        "- src/feature_x.py.md\n\n"
        "## Page Metadata\n"
        "saved_at: 2026-04-29 10:00:00 UTC\n"
        "updated_at: 2026-04-29 10:00:00 UTC\n"
    )

    (vault / "index.md").write_text(
        "| File | Description |\n"
        "|------|-------------|\n"
        "| [[queries/how-does-feature-x-work]] | Explains feature X ⚠ stale |\n"
    )

    log_entries = []
    from unittest.mock import MagicMock
    llm_client = MagicMock()
    config = MagicMock()

    with patch(
        "codebase_wiki_builder.lint_staleness.run_query",
        side_effect=typer.Exit(code=3),
    ):
        lint_result = resolve_stale_pages(vault, llm_client, config, log_entries.append)

    content = page_path.read_text(encoding="utf-8")
    # (a) Canonical answer text
    assert "this question cannot be answered by the wiki or the codebase" in content
    # (b) Unknowable banner after H1
    lines = content.splitlines()
    assert lines[0] == "# How does feature X work?"
    assert "> [!error] Unknowable" in content
    # (c) index shows ⊘ unknowable
    index_content = (vault / "index.md").read_text(encoding="utf-8")
    assert "⊘ unknowable" in index_content
    assert "⚠ stale" not in index_content
    # (d) lint-unknowable log
    log_text = "\n".join(log_entries)
    assert "lint-unknowable" in log_text
    # (e) lint did not abort
    assert len(lint_result.unknowable_pages) == 1
    assert len(lint_result.resolved_pages) == 0
```

---

### `resolve_stale_pages()` — no abort on unknowable (AT-15(e))

```python
def test_lint_continues_after_unknowable(tmp_path):
    import typer
    from unittest.mock import patch, MagicMock
    from codebase_wiki_builder.lint_staleness import resolve_stale_pages
    from codebase_wiki_builder.query_engine import QueryResult

    vault = tmp_path / "vault"
    vault.mkdir()
    queries_dir = vault / "queries"
    queries_dir.mkdir()

    # Two stale pages
    page1 = queries_dir / "unknowable.md"
    page2 = queries_dir / "resolvable.md"

    for page, slug in [(page1, "unknowable"), (page2, "resolvable")]:
        page.write_text(
            f"# Question about {slug}?\n\n"
            "> [!warning] Stale Content\n"
            "> Sources changed.\n\n"
            f"Old answer.\n\n"
            "## Sources\n- src/foo.py.md\n\n"
            "## Page Metadata\nsaved_at: 2026-01-01 00:00:00 UTC\nupdated_at: 2026-01-01 00:00:00 UTC\n"
        )

    (vault / "index.md").write_text(
        "| File | Description |\n|------|-------------|\n"
        "| [[queries/unknowable]] | Q1 ⚠ stale |\n"
        "| [[queries/resolvable]] | Q2 ⚠ stale |\n"
    )

    fresh_result = QueryResult(
        answer="Fresh answer.\n\n## Sources\n- src/foo.py.md",
        sources=["src/foo.py.md"],
        one_line_summary="Fresh summary",
        stale_warnings=[],
    )

    call_count = [0]
    def mock_run_query(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise typer.Exit(code=3)   # first page: unknowable
        return fresh_result            # second page: resolved

    log_entries = []
    llm_client = MagicMock()
    config = MagicMock()

    with patch("codebase_wiki_builder.lint_staleness.run_query", side_effect=mock_run_query):
        lint_result = resolve_stale_pages(vault, llm_client, config, log_entries.append)

    # Both pages processed
    assert len(lint_result.unknowable_pages) == 1
    assert len(lint_result.resolved_pages) == 1
    # Total run_query calls = 2 (one per page)
    assert call_count[0] == 2
```

---

### `resolve_stale_pages()` — log entry ordering (AT-14(d))

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| `lint-query` before re-run | Intercept call order | `lint-query` log entry appears before `lint-resolved` entry in `log_entries` | FR-8.1(c): logged before running |
| `lint-query` suppresses standard query log | Mock `run_query` (which doesn't call log_fn) | Only `lint-query` and `lint-resolved` in log; no `query` or `query-saved` | AT-14(d) |

---

## Notes

- **`run_query()` raises `typer.Exit` for code-3 (not a return value)**: The query engine uses `typer.Exit(code=3)` to signal "no relevant files found". Lint must catch this specifically — not suppress all `typer.Exit` — because `typer.Exit(code=1)` (index missing) is a genuine fatal error that should propagate. The `_run_internal_query()` wrapper handles this cleanly.

- **`_strip_stale_banners()` handles duplicate banners**: FR-8.1(a) explicitly requires removing ALL stale banners. This handles the edge case where a page received two banners (e.g., ingest ran twice while stale). The loop processes the content line by line and removes every matching block.

- **`read_query_page()` is called on the original file before stripping**: `read_query_page()` (item 12) is used to parse `saved_at`, `updated_at`, `question`, and `sources` from the file. The stripping of stale banners happens to the `raw_content` in memory; the file is only written once (after building the new content). This avoids a partial-write scenario.

- **`updated_at` is updated for both resolved and unknowable pages**: Both branches call `_utc_now()` to get the current timestamp and pass it to `_build_resolved_page()` / `_build_unknowable_page()`. The `saved_at` from `page.saved_at` is always preserved.

- **`_update_index_row()` does a string-based row replacement**: The index format is a simple two-column markdown table. String replacement is robust enough for this format since `[[wikilink]]` targets are unique per row. A regex-based cell replacement handles the description update safely.

- **`_console` is module-level**: Using a module-level `Console()` instance is the same pattern as other modules. The lint CLI (item 17) does not need to configure the console — all output formatting is handled here using `rich` markup.

- **The `log_fn` must be passed in, not constructed here**: `lint_staleness.py` never imports `append_log_md` directly. The caller (lint CLI, item 17) constructs `log_fn = partial(append_log_md, vault_root)` and passes it in. This matches the established pattern from items 8, 9, 12 and keeps the module decoupled from vault-path specifics in the logging path.

- **Final summary counts only resolved pages**: Per FR-8.1 step 4: "print 'Staleness resolved: N pages updated.' (where N counts only pages that were successfully regenerated, not pages marked unknowable)". The `resolved_pages` list is the correct count. Unknowable pages are displayed individually with `⊘` but not counted in the summary line.

- **No `lint-query` log entry if `read_query_page()` fails**: If reading the stale page fails, we skip it entirely (logged at ERROR, added to `skipped_pages`). We do not attempt a query re-run for a page we cannot read — so no `lint-query` entry is emitted for that page. This is the correct behavior: `lint-query` signals an attempt was made, not that a page exists.
