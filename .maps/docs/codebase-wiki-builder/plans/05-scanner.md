# Implementation Plan: File Discovery and Change-Set Computation (Ingest Phase 1)

## Spec Context

This plan implements the ingest Phase 1 scanner: the component responsible for computing the complete change-set before any vault writes occur. It fulfills FR-3 (mandatory two-phase ingest approach), FR-3.2 (file discovery and filtering), FR-3.3 (change detection via MD5 hashing), and FR-3.7 (deleted summary detection in Phase 1). The output of this module — a `ChangeSet` dataclass — is the sole input to Phase 2 (summarizer, deletions, index, staleness); nothing in Phase 2 is allowed to run before `scan_codebase()` completes successfully.

Catalog item: 5 — File Discovery and Change-Set Computation (Ingest Phase 1)
Specification section: FR-3 preamble (two-phase approach), FR-3.1 (directory mirroring), FR-3.2 (discovery and filtering), FR-3.3 (change detection), FR-3.7 (deleted file detection in Phase 1)
Acceptance criteria addressed: Phase 1 produces change-set without vault writes; binary exclusion; excluded-directory exclusion; size-threshold filtering; MD5-based new/modified detection; deleted summary detection; no vault files created or modified during Phase 1.

## Dependencies

- **Blocked by**: Item 2 (Configuration Model) — needs `WikiConfig` (codebase path, size threshold); Item 4 (Vault File Utilities) — needs `EXCLUDED_DIRS`, `BINARY_EXTENSIONS`, `is_binary_file()`, `vault_path_for_source()`, `source_path_from_vault()`, `compute_md5()`, `extract_stored_md5()`
- **Blocks**: Item 6 (Summarizer — Ingest Phase 2 Core) — `scan_codebase()` return value is Phase 2's sole input
- **Uses**: `pathlib` (stdlib), `dataclasses` (stdlib), `logging` (stdlib); `vault.py` from item 4; `config.py` from item 2

## File Changes

### New Files

- `codebase_wiki_builder/scanner.py` — `ChangeSet` dataclass; `scan_codebase()` function; internal filtering helpers; skipped-file logging helpers

### Modified Files

- None (scanner is a standalone new module)

---

## Implementation Details

### `scanner.py`

**File**: `codebase_wiki_builder/scanner.py`

**Exports**:
- `ChangeSet` — dataclass describing the complete Phase 1 result
- `scan_codebase(config, vault_root, logger) -> ChangeSet` — main Phase 1 entry point

---

### `ChangeSet` Dataclass

`ChangeSet` is the immutable record of Phase 1 results. It is passed verbatim to every Phase 2 component and must contain all information those components need: which source files are new, which are modified, which vault summaries are to be deleted, and which files were skipped (with reasons, for progress display and logging).

```python
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ChangeSet:
    # Source files (absolute paths under codebase_root) that have no
    # corresponding vault summary yet — must be summarized.
    new_files: list[Path] = field(default_factory=list)

    # Source files whose current MD5 differs from the stored MD5 in
    # the existing vault summary — must be re-summarized.
    modified_files: list[Path] = field(default_factory=list)

    # Vault summary paths (absolute, under vault_root) whose corresponding
    # source file no longer exists in the codebase — must be deleted in Phase 2.
    deleted_summaries: list[Path] = field(default_factory=list)

    # Source files skipped because they exceed the configured size threshold.
    # Stored for progress display and log.md reporting.
    skipped_too_large: list[Path] = field(default_factory=list)

    # Source files skipped because they are binary (extension match,
    # null-byte, or UTF-8 decode failure).
    skipped_binary: list[Path] = field(default_factory=list)

    # Source files skipped because they already have an up-to-date summary
    # (current MD5 matches stored MD5). Not written to log.md but used for
    # progress display.
    skipped_unchanged: list[Path] = field(default_factory=list)
```

**Design note**: `deleted_summaries` stores vault summary paths (not source paths) because Phase 2 (deletions, staleness) needs to operate directly on vault file paths. Storing vault paths here avoids recomputing them in Phase 2.

---

### `scan_codebase()` Function

**Signature**:

```python
def scan_codebase(
    config: WikiConfig,
    vault_root: Path,
    logger: logging.Logger,
) -> ChangeSet:
```

**Parameters**:
- `config` — `WikiConfig` instance (provides `codebase_path` and `file_size_threshold`)
- `vault_root` — absolute `Path` to the vault root directory
- `logger` — the application logger (from `setup_logging()` in item 4); used for DEBUG-level per-file events

**Returns**: A fully-populated `ChangeSet`. Never raises; all per-file errors are logged and the file is treated as skipped.

**Algorithm**:

Phase 1 is a two-pass operation:

**Pass 1 — Discover eligible source files and classify them**

1. Walk the codebase recursively using `Path.rglob("*")` or `os.walk()`. For each directory encountered, skip it entirely if its name is in `EXCLUDED_DIRS` (from `vault.py`). This avoids descending into `.git/`, `.venv/`, `node_modules/`, `__pycache__/`, `.maps/`.
2. For each file found:
   a. If it is a directory, skip (only process files).
   b. Apply **binary filter**: call `is_binary_file(file)`. If True → append to `change_set.skipped_binary`, log DEBUG, continue.
   c. Apply **size filter**: `file.stat().st_size > config.file_size_threshold` → append to `change_set.skipped_too_large`, log WARNING (spec requires a warning log for oversized files), continue.
   d. **Compute current MD5**: call `compute_md5(file)`.
   e. **Compute expected vault summary path**: call `vault_path_for_source(file, codebase_root, vault_root)`.
   f. **Extract stored MD5**: call `extract_stored_md5(vault_summary_path)`.
   g. Classify:
      - Stored MD5 is `None` (no summary exists) → append `file` to `change_set.new_files`.
      - Stored MD5 matches current MD5 → append `file` to `change_set.skipped_unchanged`.
      - Stored MD5 does not match current MD5 → append `file` to `change_set.modified_files`.

**Pass 2 — Detect deleted summaries**

Walk the vault directory to enumerate all existing summary files. For each summary file found, determine whether its corresponding source file still exists in the codebase. Summary files that have no living source file are added to `change_set.deleted_summaries`.

The vault walk must:
- Exclude vault-level special files from consideration: `index.md`, `log.md`, `overview.md`, `lint-report.md`
- Exclude the `logs/` and `queries/` directories entirely
- Exclude any file named `overview.md` found in any vault subdirectory
- Only consider files ending in `.md` that mirror codebase source file paths

The reverse mapping from vault summary path to source path is provided by `source_path_from_vault()` (from `vault.py`). After computing the expected source path, check `source_path.exists()`. If not, append the vault summary path to `change_set.deleted_summaries`.

**Important**: Pass 2 must complete before this function returns. The spec requires that `deleted_summaries` is fully populated before Phase 2 begins, so that staleness detection (item 8) can correctly flag query pages that reference summaries which are about to be deleted.

```python
def scan_codebase(
    config: WikiConfig,
    vault_root: Path,
    logger: logging.Logger,
) -> ChangeSet:
    codebase_root = Path(config.codebase_path)
    change_set = ChangeSet()

    # --- Pass 1: classify source files ---
    _discover_source_files(codebase_root, vault_root, config, change_set, logger)

    # --- Pass 2: detect deleted summaries ---
    _detect_deleted_summaries(vault_root, codebase_root, change_set, logger)

    logger.info(
        "Phase 1 complete: new=%d modified=%d deleted=%d "
        "unchanged=%d too_large=%d binary=%d",
        len(change_set.new_files),
        len(change_set.modified_files),
        len(change_set.deleted_summaries),
        len(change_set.skipped_unchanged),
        len(change_set.skipped_too_large),
        len(change_set.skipped_binary),
    )
    return change_set
```

---

### `_discover_source_files()` Internal Helper

```python
def _discover_source_files(
    codebase_root: Path,
    vault_root: Path,
    config: WikiConfig,
    change_set: ChangeSet,
    logger: logging.Logger,
) -> None:
```

Implements Pass 1 (source file discovery and classification). Uses `os.walk()` rather than `Path.rglob()` because `os.walk()` provides the `dirnames` list in-place, allowing pruning of excluded directories before descent:

```python
import os

for dirpath, dirnames, filenames in os.walk(codebase_root):
    # Prune excluded directories in-place (modifies dirnames before os.walk descends)
    dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]

    current_dir = Path(dirpath)
    for filename in filenames:
        file = current_dir / filename
        _classify_source_file(file, codebase_root, vault_root, config, change_set, logger)
```

Pruning `dirnames` in-place is the canonical way to prevent `os.walk()` from descending into excluded directories. This is more efficient than checking ancestry of every file path.

---

### `_classify_source_file()` Internal Helper

```python
def _classify_source_file(
    file: Path,
    codebase_root: Path,
    vault_root: Path,
    config: WikiConfig,
    change_set: ChangeSet,
    logger: logging.Logger,
) -> None:
```

Classifies a single source file by applying the filter chain (binary → size → MD5) and appending it to the appropriate `ChangeSet` list. Contains the inner logic of Pass 1 step 2 described above.

Key implementation details:
- The binary check (`is_binary_file()`) runs before the size check because `is_binary_file()` reads only 8,192 bytes (cheap), while `file.stat()` is cheap but we want to skip binary files entirely even if they happen to be small.
- The size check uses `file.stat().st_size` (one syscall) rather than reading the file.
- `compute_md5()` is called only for files that pass both filters (avoid hashing files that will be skipped anyway).
- If `file.stat()` raises `OSError` (e.g., permission denied, broken symlink), log at WARNING level and treat the file as skipped-binary (same policy as `is_binary_file()` on unreadable files).

```python
def _classify_source_file(
    file: Path,
    codebase_root: Path,
    vault_root: Path,
    config: WikiConfig,
    change_set: ChangeSet,
    logger: logging.Logger,
) -> None:
    # Binary check
    if is_binary_file(file):
        logger.debug("Skipping binary: %s", file)
        change_set.skipped_binary.append(file)
        return

    # Size check
    try:
        size = file.stat().st_size
    except OSError as exc:
        logger.warning("Cannot stat %s: %s — treating as binary/skip", file, exc)
        change_set.skipped_binary.append(file)
        return

    if size > config.file_size_threshold:
        logger.warning("Skipping oversized file (%d bytes > %d): %s",
                       size, config.file_size_threshold, file)
        change_set.skipped_too_large.append(file)
        return

    # MD5 comparison
    current_md5 = compute_md5(file)
    vault_summary = vault_path_for_source(file, codebase_root, vault_root)
    stored_md5 = extract_stored_md5(vault_summary)

    if stored_md5 is None:
        logger.debug("New file (no summary): %s", file)
        change_set.new_files.append(file)
    elif stored_md5 == current_md5:
        logger.debug("Unchanged (MD5 match): %s", file)
        change_set.skipped_unchanged.append(file)
    else:
        logger.debug("Modified (MD5 mismatch): %s", file)
        change_set.modified_files.append(file)
```

---

### `_detect_deleted_summaries()` Internal Helper

```python
def _detect_deleted_summaries(
    vault_root: Path,
    codebase_root: Path,
    change_set: ChangeSet,
    logger: logging.Logger,
) -> None:
```

Implements Pass 2 (deleted summary detection). Walks the vault directory tree to enumerate all `.md` files that represent source file summaries, then checks whether the corresponding source file still exists.

**Vault files excluded from consideration** (must not be treated as source summaries):
- `index.md`, `log.md`, `overview.md`, `lint-report.md` at the vault root
- Any file named `overview.md` anywhere in the vault tree
- All files under `logs/` directory
- All files under `queries/` directory
- Files that do not end in `.md`

**Walk strategy**: Uses `os.walk()` over the vault root, pruning `logs/` and `queries/` directories in-place (same `dirnames[:]` technique as Pass 1). For each `.md` file that passes the exclusion checks, call `source_path_from_vault()` and check source existence.

The exclusion constants `VAULT_SPECIAL_FILES` and `VAULT_EXCLUDED_DIRS` are imported from `vault.py` (item 4). No local copies are defined in `scanner.py`.

```python
from codebase_wiki_builder.vault import (
    EXCLUDED_DIRS,
    VAULT_SPECIAL_FILES,
    VAULT_EXCLUDED_DIRS,
    is_binary_file,
    vault_path_for_source,
    source_path_from_vault,
    compute_md5,
    extract_stored_md5,
)


def _detect_deleted_summaries(
    vault_root: Path,
    codebase_root: Path,
    change_set: ChangeSet,
    logger: logging.Logger,
) -> None:
    for dirpath, dirnames, filenames in os.walk(vault_root):
        # Prune logs/ and queries/ from vault walk
        dirnames[:] = [d for d in dirnames if d not in VAULT_EXCLUDED_DIRS]

        current_dir = Path(dirpath)
        for filename in filenames:
            if not filename.endswith(".md"):
                continue

            # Skip vault-root special files
            if current_dir == vault_root and filename in VAULT_SPECIAL_FILES:
                continue

            # Skip any overview.md in subdirectories
            if filename == "overview.md":
                continue

            summary_path = current_dir / filename
            source_path = source_path_from_vault(summary_path, vault_root, codebase_root)

            if not source_path.exists():
                logger.debug("Deleted summary detected: %s (source gone: %s)",
                             summary_path, source_path)
                change_set.deleted_summaries.append(summary_path)
```

---

## Error Handling

| Condition | Location | Behavior |
|-----------|----------|----------|
| `file.stat()` raises `OSError` (unreadable file, broken symlink) | `_classify_source_file()` | Log WARNING; append to `skipped_binary`; continue |
| `is_binary_file()` reads unreadable file | `vault.py` (item 4) | Returns `True` (unreadable = treat as binary) — handled transparently |
| `compute_md5()` raises `OSError` | `vault.py` (item 4) | Propagates `OSError`; caller `_classify_source_file()` does not catch it. This is a rare race condition (file readable at stat but gone before hash). Log at WARNING and append to `skipped_binary` — add a `try/except OSError` around `compute_md5()` call in `_classify_source_file()`. |
| `source_path_from_vault()` cannot reconstruct source path | `vault.py` (item 4) | Raises `ValueError` if the summary path is not under `vault_root`. Should not occur in normal operation (we start from `os.walk(vault_root)`); if it does, log at ERROR and skip that summary. |
| `extract_stored_md5()` on unreadable summary | `vault.py` (item 4) | Returns `None` — file is treated as new (will be re-summarized in Phase 2). Acceptable: Phase 2 will overwrite with a fresh summary. |
| `codebase_root` does not exist or is not a directory | `scan_codebase()` entry | `os.walk()` on a non-existent path yields nothing (no error). Caller (CLI, item 9) must validate `codebase_root.is_dir()` before calling `scan_codebase()`. The scanner is not responsible for this validation — it is already done by `load_config()` / `_validate()` in item 2. |

**`compute_md5()` race condition handling** (added detail):

```python
    # MD5 comparison — guard against rare race condition
    try:
        current_md5 = compute_md5(file)
    except OSError as exc:
        logger.warning("Cannot read %s for MD5 computation: %s — skipping", file, exc)
        change_set.skipped_binary.append(file)
        return
```

---

## Unit Test Specifications

**File**: `tests/test_scanner.py`

All tests use `tmp_path` for both a fake codebase directory and a fake vault directory. No real vault or codebase is required.

---

### `ChangeSet` dataclass

| Case | Action | Expected | Why |
|------|--------|----------|-----|
| Default construction | `ChangeSet()` | All lists are empty `[]` | `field(default_factory=list)` |
| Fields are independent | Append to `new_files`; check others | Other lists unaffected | No shared mutable default |

---

### `scan_codebase()` — new file detection

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Single new source file | Codebase has `foo.py`; vault has no summary | `change_set.new_files == [foo.py abs path]` | No stored MD5 → new |
| Multiple new files | Codebase has `a.py`, `b.py`; empty vault | Both in `new_files` | FR-3.3 |
| New file at codebase root | `codebase/main.py`; empty vault | `main.py` in `new_files` | Root-level files included |
| New file in subdirectory | `codebase/src/auth/login.py`; vault lacks summary | In `new_files` | Subdirectory traversal |

---

### `scan_codebase()` — unchanged file detection

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Unchanged file | `foo.py` exists; vault has `foo.py.md` with matching MD5 footer | `skipped_unchanged == [foo.py]`; `new_files` and `modified_files` empty | FR-3.3: MD5 match = skip |
| Matching MD5 in footer | Compute `compute_md5(foo.py)` and write that into the footer | Classified as unchanged | Footer extraction and comparison |

---

### `scan_codebase()` — modified file detection

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Modified file | `foo.py` exists; vault has `foo.py.md` with *stale* MD5 footer | `change_set.modified_files == [foo.py]` | FR-3.3: MD5 mismatch = modified |
| Footer present but wrong hash | Write `<!-- md5: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa -->` (wrong) | In `modified_files` | Mismatch detection |

---

### `scan_codebase()` — binary filtering

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Known binary extension | `image.png` in codebase | In `skipped_binary`; not in `new_files` | FR-3.2: extension match |
| `.pyc` file | `module.pyc` in codebase | In `skipped_binary` | FR-3.2 |
| File with null byte | File containing `b"text\x00more"` | In `skipped_binary` | FR-3.2: null-byte detection |
| Non-UTF-8 bytes | File with `b"\xff\xfe"` header | In `skipped_binary` | FR-3.2: decode failure |

---

### `scan_codebase()` — size filtering

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| File exactly at threshold | File size == `config.file_size_threshold` | NOT in `skipped_too_large` (size must exceed, not equal) | FR-3.2: "exceeds" threshold |
| File one byte over threshold | File size == `config.file_size_threshold + 1` | In `skipped_too_large` | FR-3.2 |
| Large file, no summary | 200 KB file; threshold 100,000 | In `skipped_too_large`; NOT in `new_files` | Skipped before MD5 |

---

### `scan_codebase()` — excluded directories

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| `.git/` excluded | `codebase/.git/HEAD` exists | HEAD not in any change_set list | FR-3.2 |
| `.venv/` excluded | `codebase/.venv/lib/x.py` | Not scanned | FR-3.2 |
| `node_modules/` excluded | `codebase/node_modules/pkg/index.js` | Not scanned | FR-3.2 |
| `__pycache__/` excluded | `codebase/src/__pycache__/foo.cpython-310.pyc` | Not scanned | FR-3.2 |
| `.maps/` excluded | `codebase/.maps/data.json` | Not scanned | FR-3.2 |
| Non-excluded subdirectory | `codebase/src/auth/login.py` | Scanned and classified | Only exact names excluded |

---

### `scan_codebase()` — deleted summary detection

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Summary with no source | Vault has `foo.py.md`; codebase has no `foo.py` | `change_set.deleted_summaries` contains vault path for `foo.py.md` | FR-3.7: Phase 1 detects deletions |
| Summary with living source | Vault has `bar.py.md`; codebase has `bar.py` | NOT in `deleted_summaries` | Source still exists |
| `index.md` not treated as summary | Vault has `index.md` | NOT in `deleted_summaries` | Special file excluded |
| `log.md` not treated as summary | Vault has `log.md` | NOT in `deleted_summaries` | Special file excluded |
| `overview.md` not treated as summary | Vault root has `overview.md` | NOT in `deleted_summaries` | Special file excluded |
| Subdirectory `overview.md` not treated as summary | `vault/src/overview.md` | NOT in `deleted_summaries` | All `overview.md` excluded |
| `lint-report.md` not treated as summary | Vault has `lint-report.md` | NOT in `deleted_summaries` | Special file excluded |
| `queries/` directory excluded | `vault/queries/how-does-auth-work.md` | NOT in `deleted_summaries` | Queries dir pruned |
| `logs/` directory excluded | `vault/logs/2026-04-29_10-00-00.log` | NOT in `deleted_summaries` | Logs dir pruned |
| Summary in subdirectory | `vault/src/auth/login.py.md`; no `codebase/src/auth/login.py` | In `deleted_summaries` with full vault path | Nested summary handling |

---

### `scan_codebase()` — Phase 1 no-write guarantee

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Vault unchanged after scan | Vault has existing summaries; run `scan_codebase()` | Vault directory tree identical before and after | Phase 1 must not write |

```python
def test_phase1_no_vault_writes(tmp_path):
    """scan_codebase() must not modify the vault in any way."""
    import os
    from codebase_wiki_builder.scanner import scan_codebase

    codebase = tmp_path / "codebase"
    vault = tmp_path / "vault"
    codebase.mkdir()
    vault.mkdir()

    # Create a source file
    (codebase / "hello.py").write_text("print('hello')")

    # Snapshot vault state before
    def vault_snapshot(v: Path) -> dict:
        result = {}
        for root, dirs, files in os.walk(v):
            for f in files:
                p = Path(root) / f
                result[str(p)] = p.read_text(encoding="utf-8", errors="replace")
        return result

    before = vault_snapshot(vault)
    config = _make_config(str(codebase))
    scan_codebase(config, vault, _make_logger())
    after = vault_snapshot(vault)

    assert before == after, "scan_codebase() modified the vault during Phase 1"
```

---

### Key Scenario: Complete classification of a mixed codebase

**Setup**: Create a fake codebase containing:
- `main.py` — new (no vault summary)
- `utils.py` — unchanged (vault summary with matching MD5)
- `auth.py` — modified (vault summary with stale MD5)
- `logo.png` — binary (extension match)
- `huge.py` — oversized (exceeds threshold)
- `deleted_old.py` was previously summarized but is now gone from codebase (`vault/deleted_old.py.md` exists)

**Action**: Call `scan_codebase(config, vault_root, logger)`.

**Expected**:
- `change_set.new_files` contains `main.py`
- `change_set.modified_files` contains `auth.py`
- `change_set.skipped_unchanged` contains `utils.py`
- `change_set.skipped_binary` contains `logo.png`
- `change_set.skipped_too_large` contains `huge.py`
- `change_set.deleted_summaries` contains the vault path for `deleted_old.py.md`
- Total files accounted for: 6 (all inputs classified)

```python
def test_complete_mixed_codebase(tmp_path):
    import hashlib, logging
    from codebase_wiki_builder.scanner import scan_codebase
    from codebase_wiki_builder.config import WikiConfig

    codebase = tmp_path / "codebase"
    vault = tmp_path / "vault"
    codebase.mkdir()
    vault.mkdir()

    # New file — no summary
    (codebase / "main.py").write_text("print('main')")

    # Unchanged file — matching MD5 in summary footer
    utils_content = "def util(): pass"
    (codebase / "utils.py").write_text(utils_content)
    utils_md5 = hashlib.md5(utils_content.encode()).hexdigest()
    (vault / "utils.py.md").write_text(
        f"# utils.py\n\nsome summary\n\n<!-- md5: {utils_md5} -->"
    )

    # Modified file — stale MD5 in summary footer
    (codebase / "auth.py").write_text("def authenticate(): ...")
    (vault / "auth.py.md").write_text(
        "# auth.py\n\nold summary\n\n<!-- md5: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa -->"
    )

    # Binary file
    (codebase / "logo.png").write_bytes(b"\x89PNG\r\n")

    # Oversized file
    threshold = 100_000
    (codebase / "huge.py").write_text("x" * (threshold + 1))

    # Deleted: vault summary exists but source does not
    (vault / "deleted_old.py.md").write_text(
        "# deleted_old.py\n\nold summary\n\n<!-- md5: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb -->"
    )

    config = WikiConfig(codebase_path=str(codebase), file_size_threshold=threshold)
    logger = logging.getLogger("test")
    change_set = scan_codebase(config, vault, logger)

    assert len(change_set.new_files) == 1
    assert change_set.new_files[0].name == "main.py"

    assert len(change_set.modified_files) == 1
    assert change_set.modified_files[0].name == "auth.py"

    assert len(change_set.skipped_unchanged) == 1
    assert change_set.skipped_unchanged[0].name == "utils.py"

    assert len(change_set.skipped_binary) == 1
    assert change_set.skipped_binary[0].name == "logo.png"

    assert len(change_set.skipped_too_large) == 1
    assert change_set.skipped_too_large[0].name == "huge.py"

    assert len(change_set.deleted_summaries) == 1
    assert change_set.deleted_summaries[0].name == "deleted_old.py.md"
```

---

## Notes

- **`os.walk()` vs `Path.rglob()`**: `os.walk()` is used for both passes because it provides `dirnames` as a mutable list, enabling `dirnames[:] = [...]` pruning before descent. With `Path.rglob()`, the only way to exclude directories is to check each file's path ancestry — O(depth) string operations per file. `os.walk()` with in-place pruning is both more efficient and more explicit.

- **Separate passes, not one combined walk**: Pass 1 (source discovery) and Pass 2 (deleted summary detection) are separate `os.walk()` calls over different roots. Combining them into one complex walk would reduce maintainability without meaningfully improving performance — both walks are O(n) in the number of files and the codebase/vault directories are independent.

- **`deleted_summaries` stores vault paths**: Phase 2 consumers (item 7 deletions, item 8 staleness) need vault summary paths, not source paths. Storing vault paths avoids repeated `vault_path_for_source()` calls in multiple Phase 2 callers.

- **No vault writes in Phase 1**: `scan_codebase()` and its helpers contain zero write operations (`write_text`, `write_bytes`, `mkdir`, `unlink`, `rename`, etc.). This is a structural constraint enforced by code review — the function opens files only for reading (`extract_stored_md5()`, `is_binary_file()`, `compute_md5()`) and for metadata (`stat()`).

- **Size threshold is exclusive (strictly greater than)**: The spec states "exceeds the configured threshold". A file of exactly `file_size_threshold` bytes is NOT skipped. The condition is `size > config.file_size_threshold`, not `>=`.

- **Order within `ChangeSet` lists is not specified**: The lists reflect filesystem walk order, which is OS-dependent. Phase 2 consumers must not rely on list ordering for correctness. Progress display (item 9) may sort for presentation purposes, but the scanner itself imposes no ordering guarantee.

- **`VAULT_SPECIAL_FILES` check is at vault root only**: Only files directly in `vault_root/` are checked against this set (imported from `vault.py`). `overview.md` in subdirectories is excluded separately by the `filename == "overview.md"` check. This correctly handles the spec requirement: "any overview.md file in any subdirectory" is excluded.

- **`codebase_root` validation is upstream**: `scan_codebase()` trusts that `config.codebase_path` points to a readable directory. This was validated by `load_config()` / `_validate()` (item 2) and the CLI (item 9) before calling the scanner. If `os.walk()` is called on a non-existent path, it simply yields nothing — no error, no files scanned. The resulting `ChangeSet` would only have deleted summaries, which is correct behavior if the codebase vanished mid-run.

- **Logger is passed in, not imported**: `scan_codebase()` takes the logger as a parameter rather than calling `logging.getLogger()` internally. This follows the same pattern used in `logging_setup.py` (item 4) and makes the module easier to test (pass a test-configured logger or a `MagicMock`).

- **`skipped_binary` is a catch-all for unreadable files**: Files that cannot be stat'd or hashed (due to `OSError`) are appended to `skipped_binary` rather than a separate `skipped_error` list. This keeps the `ChangeSet` interface simple — Phase 2 only needs to know about the three actionable categories (new, modified, deleted). The reason for a skip is captured in the debug log, not in the data structure.
