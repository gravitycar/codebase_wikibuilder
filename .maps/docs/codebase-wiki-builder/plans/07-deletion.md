# Implementation Plan: Deletion Handling and Backlink Cleanup (Ingest Phase 2 Deletions)

## Spec Context

This plan implements the Phase 2 deletion step of the `ingest` command. It fulfills FR-3.7 (deleted file handling in Phase 2) and FR-3.1 (empty vault directory cleanup). After Phase 2 summary writes complete for new/modified files (item 6), `apply_deletions()` receives the Phase 1 change-set and: (1) deletes each vault summary file whose source is gone, (2) scans all remaining summary files for wikilinks that reference deleted paths and strips those dead backlinks, (3) removes vault directories that become empty after deletions, and (4) logs each deletion and backlink removal to both `log.md` and the debug logger.

This module is a pure filesystem mutator — it receives fully-computed information from the scanner (item 5) and vault utilities (item 4) and applies destructive changes to the vault. It does not call the LLM.

Catalog item: 7 — Deletion Handling and Backlink Cleanup (Ingest Phase 2 Deletions)
Specification section: FR-3.7 (Phase 2 deletion and backlink cleanup), FR-3.1 (empty directory cleanup after deletion)
Acceptance criteria addressed: Summary file deletion for each entry in `change_set.deleted_summaries`; O(n) scan of all remaining summaries for dead backlinks; backlink removal; empty vault directory cleanup; deletion and backlink removal logged to `log.md` and the debug log.

## Dependencies

- **Blocked by**: Item 4 (Vault File Utilities + Logging) — needs `wikilink()`, `append_log_md()`, `EXCLUDED_DIRS` patterns; Item 5 (Scanner) — needs `ChangeSet` dataclass; Item 6 (Summarizer) — Phase 2 summary writes must complete before deletions run (deletions must not delete files that were just written for new/modified sources)
- **Blocks**: Item 8 (Index Regeneration and Staleness Detection) — index rebuild and staleness scan run after deletions complete
- **Uses**: `pathlib` (stdlib), `re` (stdlib), `logging` (stdlib), `datetime` (stdlib); `ChangeSet` from `scanner.py`; `append_log_md()`, `setup_logging()` (logger passed in) from `logging_setup.py`; `wikilink()` from `vault.py`

## File Changes

### New Files

- `codebase_wiki_builder/deletion.py` — `apply_deletions()` public function; internal helpers for backlink scanning and empty directory cleanup

### Modified Files

- None

---

## Implementation Details

### `deletion.py`

**File**: `codebase_wiki_builder/deletion.py`

**Exports**:
- `apply_deletions(change_set: ChangeSet, vault_root: Path, log_fn: Callable[[str], None], logger: logging.Logger) -> DeletionResult` — main entry point; orchestrates deletion, backlink cleanup, directory pruning, and logging

**Internal helpers** (not exported):
- `_delete_summary_files(deleted_summaries: list[Path], log_fn, logger) -> list[Path]` — deletes files, returns list of vault summary paths successfully deleted
- `_collect_remaining_summaries(vault_root: Path, excluded_vault_paths: set[Path]) -> list[Path]` — enumerates all `.md` summary files in the vault that were NOT deleted
- `_build_dead_wikilinks(deleted_vault_paths: list[Path], vault_root: Path) -> set[str]` — computes the set of wikilink strings (e.g. `[[src/auth/login.py]]`) that are now dead
- `_remove_backlinks_from_file(summary_path: Path, dead_links: set[str], log_fn, logger) -> int` — scans one file for dead backlinks and rewrites it without them; returns count of backlinks removed
- `_cleanup_empty_directories(vault_root: Path, logger: logging.Logger) -> list[Path]` — walks the vault and removes any directories that are now empty (bottom-up); returns list of removed dirs

---

### `DeletionResult` Dataclass

A lightweight result record returned by `apply_deletions()`. Used by the ingest CLI (item 9) for progress display and final summary reporting.

```python
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DeletionResult:
    # Vault summary paths that were successfully deleted
    deleted_files: list[Path] = field(default_factory=list)

    # Vault summary paths that could not be deleted (OSError)
    failed_deletions: list[Path] = field(default_factory=list)

    # (vault_summary_path, count_of_backlinks_removed) for each file modified
    backlinks_cleaned: list[tuple[Path, int]] = field(default_factory=list)

    # Vault directories removed because they became empty
    removed_dirs: list[Path] = field(default_factory=list)
```

---

### `apply_deletions()` — Main Entry Point

**Signature**:

```python
def apply_deletions(
    change_set: ChangeSet,
    vault_root: Path,
    log_fn: Callable[[str], None],
    logger: logging.Logger,
) -> DeletionResult:
```

**Parameters**:
- `change_set` — Phase 1 result from `scan_codebase()`; `change_set.deleted_summaries` is the list of vault summary paths to delete
- `vault_root` — absolute `Path` to the vault root directory
- `log_fn` — callable that appends one entry to `log.md` (i.e. `lambda entry: append_log_md(vault_root, entry)`); the caller constructs this closure
- `logger` — the application-wide logger (from `setup_logging()`), used for DEBUG/INFO events to the operational debug log

**Returns**: `DeletionResult` populated with per-step outcomes.

**Algorithm**:

```
1. If change_set.deleted_summaries is empty: return empty DeletionResult immediately (no-op)
2. Step 1 — Delete summary files: call _delete_summary_files()
3. Step 2 — Collect remaining summaries: call _collect_remaining_summaries(), passing the set of deleted vault paths so they are excluded
4. Step 3 — Compute dead wikilinks: call _build_dead_wikilinks()
5. Step 4 — Remove dead backlinks: for each remaining summary, call _remove_backlinks_from_file(); accumulate results in DeletionResult.backlinks_cleaned
6. Step 5 — Clean up empty directories: call _cleanup_empty_directories()
7. Return DeletionResult
```

```python
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from codebase_wiki_builder.scanner import ChangeSet
from codebase_wiki_builder.vault import wikilink


def apply_deletions(
    change_set: ChangeSet,
    vault_root: Path,
    log_fn: Callable[[str], None],
    logger: logging.Logger,
) -> DeletionResult:
    result = DeletionResult()

    if not change_set.deleted_summaries:
        logger.debug("No deletions in change_set; skipping deletion phase")
        return result

    # Step 1: Delete summary files
    deleted_paths = _delete_summary_files(
        change_set.deleted_summaries, log_fn, logger
    )
    result.deleted_files = deleted_paths
    result.failed_deletions = [
        p for p in change_set.deleted_summaries if p not in set(deleted_paths)
    ]

    if not deleted_paths:
        # All deletions failed; no backlink cleanup needed
        logger.warning("All %d deletions failed; skipping backlink cleanup",
                       len(change_set.deleted_summaries))
        return result

    # Step 2: Collect remaining summaries (exclude deleted paths from scan)
    deleted_set = set(deleted_paths)
    remaining = _collect_remaining_summaries(vault_root, deleted_set)
    logger.debug("Backlink scan: %d remaining summary files to check", len(remaining))

    # Step 3: Build set of dead wikilink strings
    dead_links = _build_dead_wikilinks(deleted_paths, vault_root)
    logger.debug("Dead wikilinks to remove: %s", dead_links)

    # Step 4: Remove dead backlinks from remaining summaries
    for summary_path in remaining:
        count = _remove_backlinks_from_file(summary_path, dead_links, log_fn, logger)
        if count > 0:
            result.backlinks_cleaned.append((summary_path, count))

    # Step 5: Clean up empty directories
    result.removed_dirs = _cleanup_empty_directories(vault_root, logger)

    logger.info(
        "Deletion phase complete: deleted=%d failed=%d files_with_backlinks_cleaned=%d empty_dirs_removed=%d",
        len(result.deleted_files),
        len(result.failed_deletions),
        len(result.backlinks_cleaned),
        len(result.removed_dirs),
    )
    return result
```

---

### `_delete_summary_files()` Internal Helper

Iterates `deleted_summaries` and unlinks each file. Each deletion is logged to `log.md` (via `log_fn`) and the debug logger. `OSError` on any individual deletion is caught, logged at ERROR level, and the file is counted as a failed deletion (processing continues for other files).

```python
def _delete_summary_files(
    deleted_summaries: list[Path],
    log_fn: Callable[[str], None],
    logger: logging.Logger,
) -> list[Path]:
    """Delete vault summary files. Returns list of successfully deleted paths."""
    successfully_deleted: list[Path] = []
    for summary_path in deleted_summaries:
        try:
            summary_path.unlink()
            successfully_deleted.append(summary_path)
            logger.info("Deleted summary: %s", summary_path)
            ts = _utc_now()
            log_fn(f"{ts} | deletion | {summary_path.name}")
        except FileNotFoundError:
            # Already gone — treat as success (idempotent)
            successfully_deleted.append(summary_path)
            logger.debug("Summary already gone (FileNotFoundError): %s", summary_path)
        except OSError as exc:
            logger.error("Failed to delete summary %s: %s", summary_path, exc)
    return successfully_deleted
```

`FileNotFoundError` (a subclass of `OSError`) is treated as a success — if the file is already absent, the goal is achieved. Other `OSError` subtypes (permission denied, etc.) are treated as failures.

---

### `_collect_remaining_summaries()` Internal Helper

Enumerates all `.md` files in the vault that are summary files (applying the same exclusion logic as the scanner's `_detect_deleted_summaries()` in item 5) and were not deleted in the current run.

```python
from codebase_wiki_builder.vault import VAULT_SPECIAL_FILES, VAULT_EXCLUDED_DIRS


def _collect_remaining_summaries(
    vault_root: Path,
    excluded_vault_paths: set[Path],
) -> list[Path]:
    """Return all .md summary files in the vault that were not deleted this run."""
    import os

    remaining: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(vault_root):
        dirnames[:] = [d for d in dirnames if d not in VAULT_EXCLUDED_DIRS]

        current_dir = Path(dirpath)
        for filename in filenames:
            if not filename.endswith(".md"):
                continue
            if current_dir == vault_root and filename in VAULT_SPECIAL_FILES:
                continue
            if filename == "overview.md":
                continue

            full_path = current_dir / filename
            if full_path in excluded_vault_paths:
                continue

            remaining.append(full_path)

    return remaining
```

The exclusion rules mirror those in `scanner._detect_deleted_summaries()` exactly. This ensures the backlink scan covers only the same file set that the scanner considers to be summaries — no special files, no query pages, no log files.

---

### `_build_dead_wikilinks()` Internal Helper

For each successfully deleted vault summary path, computes the Obsidian wikilink string that other summaries would use to reference it. The dead wikilinks set is what the backlink scanner searches for.

```python
def _build_dead_wikilinks(
    deleted_vault_paths: list[Path],
    vault_root: Path,
) -> set[str]:
    """Build the set of wikilink strings for deleted summaries.

    Example: if vault/src/auth/login.py.md was deleted,
    the dead wikilink is '[[src/auth/login.py]]'.
    """
    dead: set[str] = set()
    for summary_path in deleted_vault_paths:
        link = wikilink(summary_path, vault_root)
        dead.add(link)
    return dead
```

`wikilink()` from `vault.py` returns the Obsidian wikilink string without `.md` extension, e.g. `[[src/auth/login.py]]`. This is the format used in `## References` sections by the summarizer (item 6). The backlink scanner uses exact string matching against this set.

---

### `_remove_backlinks_from_file()` Internal Helper

Reads one summary file, identifies lines in its `## References` section that contain a dead wikilink, removes those lines, and writes the file back if any were removed. Returns the count of backlinks removed.

**Backlink format**: The summarizer (item 6) writes reference lines as:
- `- [[relative/path/to/file]]` (explicit)
- `- [[relative/path/to/file]] (inferred)` (dynamic)

The scanner strips only lines that contain a dead wikilink string. It does not parse the full markdown structure — it performs line-level substring matching within the `## References` section.

```python
def _remove_backlinks_from_file(
    summary_path: Path,
    dead_links: set[str],
    log_fn: Callable[[str], None],
    logger: logging.Logger,
) -> int:
    """Remove dead backlink lines from a summary file.

    Only removes lines inside the ## References section that contain a dead wikilink.
    Returns the number of backlink lines removed.
    """
    try:
        content = summary_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Cannot read %s for backlink cleanup: %s", summary_path, exc)
        return 0

    lines = content.splitlines(keepends=True)
    new_lines: list[str] = []
    in_references_section = False
    removed_count = 0

    for line in lines:
        stripped = line.rstrip("\n").rstrip()

        # Track entry into ## References section
        if stripped == "## References":
            in_references_section = True
            new_lines.append(line)
            continue

        # Exit ## References on any new ## heading
        if in_references_section and stripped.startswith("## ") and stripped != "## References":
            in_references_section = False

        # Within References: check if this line contains a dead wikilink
        if in_references_section:
            is_dead = any(dead_link in stripped for dead_link in dead_links)
            if is_dead:
                removed_count += 1
                logger.debug(
                    "Removing dead backlink in %s: %r", summary_path.name, stripped
                )
                # Do not append this line — it is dropped
                continue

        new_lines.append(line)

    if removed_count == 0:
        return 0

    # Rewrite the file without the dead backlink lines
    try:
        summary_path.write_text("".join(new_lines), encoding="utf-8")
        ts = _utc_now()
        log_fn(f"{ts} | backlink-removed | {summary_path.name} ({removed_count} link(s) removed)")
        logger.info(
            "Cleaned %d dead backlink(s) from %s", removed_count, summary_path.name
        )
    except OSError as exc:
        logger.error("Cannot rewrite %s after backlink cleanup: %s", summary_path, exc)
        return 0  # Report as zero since cleanup did not persist

    return removed_count
```

**Design notes**:
- The function uses line-level iteration with a section-tracking flag (`in_references_section`) rather than a full markdown parser. This is intentional: the summary format is strictly defined by the summarizer (item 6), making a lightweight approach reliable.
- Section tracking exits on any `## ` heading (other than `## References` itself). Since the MD5 footer `<!-- md5: ... -->` is not a heading, it is preserved correctly.
- Dead link detection uses `any(dead_link in stripped for dead_link in dead_links)` — substring match. Since wikilinks have a distinctive format (`[[...]]`), false positives within description or footer text are extremely unlikely.
- The entire file is read into memory before writing. Summary files are small (typically a few KB). This is acceptable for MVP.

---

### `_cleanup_empty_directories()` Internal Helper

After summary deletions, some vault directories may now be empty. This helper walks the vault bottom-up and removes any empty directories (excluding the vault root itself and the `logs/` and `queries/` directories).

```python
def _cleanup_empty_directories(
    vault_root: Path,
    logger: logging.Logger,
) -> list[Path]:
    """Remove empty vault directories created by summary deletion.

    Walks bottom-up so that a directory emptied by removing its last child
    subdirectory is itself eligible for removal in the same pass.

    Returns list of directories removed.
    """
    removed: list[Path] = []

    # os.walk with topdown=False visits children before parents (bottom-up)
    import os
    for dirpath, dirnames, filenames in os.walk(vault_root, topdown=False):
        current_dir = Path(dirpath)

        # Never remove the vault root itself
        if current_dir == vault_root:
            continue

        # Never remove logs/ or queries/ even if empty
        if current_dir.name in VAULT_EXCLUDED_DIRS:
            continue

        # Check if the directory is now empty (no files, no subdirectories)
        try:
            entries = list(current_dir.iterdir())
        except OSError as exc:
            logger.warning("Cannot inspect directory %s: %s", current_dir, exc)
            continue

        if not entries:
            try:
                current_dir.rmdir()
                removed.append(current_dir)
                logger.info("Removed empty vault directory: %s", current_dir)
            except OSError as exc:
                logger.warning("Cannot remove empty directory %s: %s", current_dir, exc)

    return removed
```

`os.walk(topdown=False)` visits deepest directories first. This means a now-empty subdirectory is removed before its parent is inspected — so the parent (which may have become empty because its only child was just removed) will also be cleaned up in the same pass, without requiring multiple iterations.

`rmdir()` is used rather than `shutil.rmtree()` — it only removes empty directories and raises `OSError` if the directory still has content, making it safe against race conditions where a file was added between `iterdir()` and `rmdir()`.

---

### `_utc_now()` Utility

```python
def _utc_now() -> str:
    """Return current UTC time formatted for log.md entries."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
```

Used by the log entry formatters in `_delete_summary_files()` and `_remove_backlinks_from_file()`. Keeps timestamp formatting consistent with the `log.md` format specified in FR-6.1.

---

### Complete Module Skeleton

```python
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from codebase_wiki_builder.scanner import ChangeSet
from codebase_wiki_builder.vault import wikilink, VAULT_SPECIAL_FILES, VAULT_EXCLUDED_DIRS


@dataclass
class DeletionResult:
    deleted_files: list[Path] = field(default_factory=list)
    failed_deletions: list[Path] = field(default_factory=list)
    backlinks_cleaned: list[tuple[Path, int]] = field(default_factory=list)
    removed_dirs: list[Path] = field(default_factory=list)


def _utc_now() -> str: ...
def _delete_summary_files(deleted_summaries, log_fn, logger) -> list[Path]: ...
def _collect_remaining_summaries(vault_root, excluded_vault_paths) -> list[Path]: ...
def _build_dead_wikilinks(deleted_vault_paths, vault_root) -> set[str]: ...
def _remove_backlinks_from_file(summary_path, dead_links, log_fn, logger) -> int: ...
def _cleanup_empty_directories(vault_root, logger) -> list[Path]: ...


def apply_deletions(
    change_set: ChangeSet,
    vault_root: Path,
    log_fn: Callable[[str], None],
    logger: logging.Logger,
) -> DeletionResult: ...
```

---

## Error Handling

| Condition | Location | Behavior |
|-----------|----------|----------|
| `summary_path.unlink()` raises `FileNotFoundError` | `_delete_summary_files()` | Treated as success (file already gone — idempotent) |
| `summary_path.unlink()` raises other `OSError` | `_delete_summary_files()` | ERROR logged; file added to `failed_deletions`; processing continues |
| `summary_path.read_text()` raises `OSError` | `_remove_backlinks_from_file()` | WARNING logged; returns 0 (file not cleaned) |
| `summary_path.write_text()` raises `OSError` after backlink removal | `_remove_backlinks_from_file()` | ERROR logged; returns 0 (cleanup did not persist) |
| `current_dir.iterdir()` raises `OSError` | `_cleanup_empty_directories()` | WARNING logged; directory skipped |
| `current_dir.rmdir()` raises `OSError` (race condition — dir not empty) | `_cleanup_empty_directories()` | WARNING logged; directory left in place |
| `change_set.deleted_summaries` is empty | `apply_deletions()` | Returns empty `DeletionResult` immediately (fast path) |
| All deletions fail | `apply_deletions()` | WARNING logged; backlink scan skipped (no dead links to clean) |

---

## Unit Test Specifications

**File**: `tests/test_deletion.py`

All tests use `tmp_path` for both a fake vault directory and a fake codebase directory. No LLM calls. No network. The `log_fn` is a simple list-appending lambda for easy assertion.

---

### `DeletionResult` dataclass

| Case | Action | Expected | Why |
|------|--------|----------|-----|
| Default construction | `DeletionResult()` | All lists are empty | `field(default_factory=list)` |

---

### `apply_deletions()` — no-op when no deletions

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Empty `deleted_summaries` | `ChangeSet()` with no deletions | Returns `DeletionResult()` with all empty lists; vault unchanged | Fast path |

---

### `apply_deletions()` — file deletion

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Single deleted summary | `vault/src/foo.py.md` exists; `change_set.deleted_summaries = [vault/src/foo.py.md]` | File no longer exists; `result.deleted_files == [vault/src/foo.py.md]` | FR-3.7: delete summary |
| Multiple deleted summaries | Two summary files in vault; both in `deleted_summaries` | Both deleted; both in `result.deleted_files` | Multiple deletions |
| Already-absent summary | Summary listed in `deleted_summaries` but file already gone | Treated as success; in `result.deleted_files`; no error | Idempotent |
| Deletion failure (unwritable) | Summary is read-only or in read-only dir | In `result.failed_deletions`; not in `result.deleted_files` | OSError handling |

---

### `apply_deletions()` — log.md entries

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Deletion logged | One summary deleted | `log_fn` called with entry containing `"deletion"` and the file name | FR-3.7: log each deletion |
| Backlink removal logged | One remaining summary has dead backlink | `log_fn` called with entry containing `"backlink-removed"` | FR-3.7: log backlink removal |
| No log entry on no-op | `deleted_summaries` empty | `log_fn` never called | Only log actual changes |

---

### `_build_dead_wikilinks()`

| Case | Input | Expected | Why |
|------|-------|----------|-----|
| Simple path | `vault/src/auth/login.py.md` deleted; `vault_root=vault/` | Returns `{"[[src/auth/login.py]]"}` | Wikilink format per FR-3.5 |
| Root-level deletion | `vault/main.py.md` deleted | Returns `{"[[main.py]]"}` | Root-level summary |
| Multiple deletions | Two deleted paths | Returns set with two wikilink strings | Set semantics |

---

### `_remove_backlinks_from_file()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| File with one dead backlink | Summary has `- [[src/auth/login.py]]` in References; that link is dead | Line removed; returns 1 | Core backlink cleanup |
| File with dead inferred backlink | `- [[src/plugins/loader.py]] (inferred)` and that link is dead | Line removed; returns 1 | FR-3.5: `(inferred)` lines also cleaned |
| File with no dead backlinks | Summary has no reference to deleted path | File unchanged; returns 0 | No false removals |
| Dead link only in References section | Dead wikilink text appears in description prose AND References | Only References line removed; prose preserved | Section-scoped removal |
| Multiple dead backlinks in one file | Two dead links in References | Both lines removed; returns 2 | Multi-link cleanup |
| Dead link adjacent to live link | References has one dead and one live link | Only dead line removed; live line preserved | Selective removal |
| File with no References section | Summary has no `## References` heading | File unchanged; returns 0 | Gracefully handles missing section |
| Unreadable file | `summary_path` has no read permission | Returns 0; WARNING logged; file not modified | OSError on read |
| Write failure after removal | File readable but not writable | Returns 0; ERROR logged | OSError on write |

**Key Scenario: Dead backlink removed, live backlink preserved**

```python
def test_remove_backlinks_preserves_live_links(tmp_path):
    import logging
    from codebase_wiki_builder.deletion import _remove_backlinks_from_file

    summary = tmp_path / "summary.py.md"
    summary.write_text(
        "# summary.py\n\nSome description.\n\n"
        "## References\n"
        "- [[src/auth/login.py]]\n"
        "- [[src/utils/helper.py]]\n\n"
        "<!-- md5: abc -->\n",
        encoding="utf-8",
    )

    dead_links = {"[[src/utils/helper.py]]"}
    logger = logging.getLogger("test")
    log_entries = []
    count = _remove_backlinks_from_file(summary, dead_links, log_entries.append, logger)

    assert count == 1
    content = summary.read_text(encoding="utf-8")
    assert "[[src/auth/login.py]]" in content
    assert "[[src/utils/helper.py]]" not in content
    assert "## References" in content
    assert "<!-- md5: abc -->" in content
```

**Key Scenario: Dead backlink in description prose not removed**

```python
def test_backlink_in_description_not_removed(tmp_path):
    import logging
    from codebase_wiki_builder.deletion import _remove_backlinks_from_file

    summary = tmp_path / "consumer.py.md"
    summary.write_text(
        "# consumer.py\n\n"
        "This module uses [[src/auth/login.py]] internally.\n\n"
        "## References\n"
        "- [[src/auth/login.py]]\n\n"
        "<!-- md5: def -->\n",
        encoding="utf-8",
    )

    dead_links = {"[[src/auth/login.py]]"}
    logger = logging.getLogger("test")
    log_entries = []
    count = _remove_backlinks_from_file(summary, dead_links, log_entries.append, logger)

    assert count == 1
    content = summary.read_text(encoding="utf-8")
    # Prose reference in description is preserved
    assert "[[src/auth/login.py]]" in content
    # The References section line is gone
    assert "## References" in content
    # Check no References-section lines remain for the dead link
    lines = content.splitlines()
    in_refs = False
    for line in lines:
        if line.strip() == "## References":
            in_refs = True
            continue
        if in_refs and line.startswith("## "):
            in_refs = False
        if in_refs:
            assert "[[src/auth/login.py]]" not in line
```

---

### `_collect_remaining_summaries()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Excludes deleted paths | Vault has 3 summaries; 1 in `excluded_vault_paths` | Returns 2 paths | Deleted files not scanned |
| Excludes `index.md` | `vault/index.md` exists | Not in result | Special file |
| Excludes `log.md` | `vault/log.md` exists | Not in result | Special file |
| Excludes `overview.md` at root | `vault/overview.md` exists | Not in result | Special file |
| Excludes `overview.md` in subdir | `vault/src/overview.md` exists | Not in result | Any `overview.md` excluded |
| Excludes `logs/` contents | `vault/logs/run.log` exists | Not in result | Logs dir pruned |
| Excludes `queries/` contents | `vault/queries/how-auth-works.md` exists | Not in result | Queries dir pruned |
| Includes nested summaries | `vault/src/auth/login.py.md` exists | In result | Real summary |

---

### `_cleanup_empty_directories()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Empty subdirectory removed | `vault/src/auth/` has no files | Directory removed; in `removed_dirs` | FR-3.1: empty dir cleanup |
| Non-empty subdirectory preserved | `vault/src/` has `main.py.md` | Not removed | Has content |
| Vault root never removed | `vault_root` is empty | Not removed | Never delete the root |
| `logs/` preserved even if empty | `vault/logs/` has no files | Not removed | Excluded from cleanup |
| `queries/` preserved even if empty | `vault/queries/` has no files | Not removed | Excluded from cleanup |
| Nested empty dirs — bottom-up | `vault/src/auth/` empty after `vault/src/auth/sub/` removed | Both removed in one pass | `topdown=False` chain |

**Key Scenario: Bottom-up cascading removal**

```python
def test_cleanup_cascades_bottom_up(tmp_path):
    import logging
    from codebase_wiki_builder.deletion import _cleanup_empty_directories

    vault = tmp_path / "vault"
    # Create a two-level nested empty directory structure
    deep = vault / "src" / "auth" / "helpers"
    deep.mkdir(parents=True)
    # No files anywhere in this subtree

    logger = logging.getLogger("test")
    removed = _cleanup_empty_directories(vault, logger)

    # All three empty directories should be removed (helpers, auth, src)
    assert not (vault / "src").exists()
    assert len(removed) == 3
```

---

### `apply_deletions()` — end-to-end integration

**Key Scenario: Complete deletion with backlink cleanup**

```python
def test_apply_deletions_end_to_end(tmp_path):
    import logging
    from codebase_wiki_builder.deletion import apply_deletions
    from codebase_wiki_builder.scanner import ChangeSet

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "src").mkdir()

    # The summary to be deleted
    login_md = vault / "src" / "login.py.md"
    login_md.write_text(
        "# src/login.py\n\nLogin logic.\n\n## References\n\n<!-- md5: aaa -->\n"
    )

    # A remaining summary that has a backlink to login.py
    consumer_md = vault / "src" / "consumer.py.md"
    consumer_md.write_text(
        "# src/consumer.py\n\nUses login.\n\n"
        "## References\n"
        "- [[src/login.py]]\n\n"
        "<!-- md5: bbb -->\n"
    )

    change_set = ChangeSet(deleted_summaries=[login_md])
    log_entries = []
    logger = logging.getLogger("test")

    result = apply_deletions(change_set, vault, log_entries.append, logger)

    # login.py.md deleted
    assert not login_md.exists()
    assert login_md in result.deleted_files

    # consumer.py.md still exists but backlink removed
    assert consumer_md.exists()
    content = consumer_md.read_text(encoding="utf-8")
    assert "[[src/login.py]]" not in content
    assert "## References" in content
    assert "<!-- md5: bbb -->" in content

    # log entries written
    assert any("deletion" in e for e in log_entries)
    assert any("backlink-removed" in e for e in log_entries)

    # src/ directory still exists (consumer.py.md is still there)
    assert (vault / "src").exists()
```

---

## Notes

- **`_remove_backlinks_from_file()` is section-scoped**: The backlink remover only removes lines inside the `## References` section. This is important because description text may mention filenames in prose (e.g. "This module uses `login.py`"), and those mentions should not be stripped. The section-tracking flag (`in_references_section`) provides this scoping without a full markdown parser.

- **O(n) scan is intentional**: The spec explicitly acknowledges this: "This is an O(n) scan across all summaries and SHALL be performed as part of every ingest that involves deletions." For MVP vault sizes (hundreds of files), this is fine. An inverted index is noted as a v2 optimization in the risk table.

- **`_build_dead_wikilinks()` uses `wikilink()` from `vault.py`**: This ensures the dead link strings match exactly what the summarizer (item 6) writes using the same `wikilink()` function. Consistency is guaranteed by sharing the same helper — no string formatting duplication.

- **Empty-directory cleanup uses `topdown=False`**: Bottom-up traversal allows cascading removal of a now-empty parent whose only child subdirectory was just removed — all in a single `os.walk()` pass. A top-down walk would require multiple iterations.

- **`logs/` and `queries/` excluded from empty-directory cleanup**: These directories may be temporarily empty (e.g., on the very first run before any queries are saved), but they serve as stable mount points and must not be removed. The `VAULT_EXCLUDED_DIRS` constant (imported from `vault.py`) is reused for both the summary collection exclusion and the directory cleanup exclusion.

- **`log_fn` is a Callable, not a direct call to `append_log_md`**: The caller (ingest CLI, item 9) constructs a closure: `log_fn = lambda entry: append_log_md(vault_root, entry)`. This keeps `deletion.py` decoupled from `logging_setup.py` — it does not need to import or know about `append_log_md` directly. The same `log_fn` pattern is used in item 8 (staleness detection).

- **Backlink write failure returns 0**: If `write_text()` fails after the backlinks are identified for removal, the function returns 0 to indicate that no cleanup was persisted. The in-memory `new_lines` list is discarded. The original file is unchanged (because `write_text()` failed before overwriting). This is the correct behavior: a partial write (e.g., truncated file) is worse than no write.

- **`FileNotFoundError` on deletion treated as success**: If a summary file is listed in `deleted_summaries` but is already gone when `unlink()` is called (e.g., deleted externally between Phase 1 scan and Phase 2 apply), this is treated as a success. The Phase 1 goal (summary does not exist) is already achieved.

- **`lint-report.md` excluded from backlink scan**: `lint-report.md` is in `_VAULT_SPECIAL_FILES` and excluded from `_collect_remaining_summaries()`. Lint report files do not contain Obsidian wikilinks in the `## References` format — they are health-check output with a different structure. Scanning them would be a no-op anyway, but explicitly excluding them keeps the scan set clean.
