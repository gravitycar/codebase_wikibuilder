# Implementation Plan: Vault File Utilities and Logging Infrastructure

## Spec Context

This plan implements shared filesystem helpers and both logging sinks used throughout the entire application. It fulfills FR-3.2 (binary file detection and excluded directory filtering), FR-3.3 (MD5 hashing), FR-3.5 (summary file naming convention `<name>.<ext>.md` and wikilink formatting), FR-6.1 (append-only `log.md`), and FR-6.2 (per-run debug log under `logs/`). Every downstream catalog item (5–13, 16, 18) imports from these two modules — they are the shared utility layer on top of which the entire application is built.

Catalog item: 4 — Vault File Utilities and Logging Infrastructure
Specification section: FR-3.2 (binary detection, excluded dirs), FR-3.3 (MD5), FR-3.5 (naming convention, wikilink format), FR-6.1 (log.md, append-only), FR-6.2 (debug log path/format)
Acceptance criteria addressed: Binary-file exclusion, excluded-directory filtering, MD5 computation, summary file naming, Obsidian wikilink formatting, slug generation, `log.md` append-only invariant, per-run debug log creation.

## Dependencies

- **Blocked by**: Item 1 (Project Scaffold) — package must exist before this module can be placed inside it
- **Blocks**: Items 5, 6, 7, 8, 9, 10, 11, 12, 13, 16, 18 (all items that do filesystem or logging work)
- **Uses**: `pathlib` (stdlib), `hashlib` (stdlib), `re` (stdlib), `logging` (stdlib), `datetime` (stdlib)

## File Changes

### New Files

- `codebase_wiki_builder/vault.py` — Path mirror logic, slug generation, MD5 hashing, wikilink formatting, binary-file detection, excluded-directory constants
- `codebase_wiki_builder/logging_setup.py` — `setup_logging()` (creates per-run debug log), `append_log_md()` (append-only `log.md` writer)

### Modified Files

- None (both are new modules; item 1 files are untouched)

## Implementation Details

---

### `vault.py`

**File**: `codebase_wiki_builder/vault.py`

**Exports**:
- `EXCLUDED_DIRS: frozenset[str]` — directory names excluded from scanning
- `BINARY_EXTENSIONS: frozenset[str]` — extensions always treated as binary
- `VAULT_SPECIAL_FILES: frozenset[str]` — vault-root `.md` files that are not source summaries (`index.md`, `log.md`, `overview.md`, `lint-report.md`)
- `VAULT_EXCLUDED_DIRS: frozenset[str]` — vault directory names excluded from summary walks (`logs`, `queries`)
- `is_binary_file(path: Path) -> bool` — True if the file should be excluded as binary
- `mirror_path(source_file: Path, codebase_root: Path, vault_root: Path) -> Path` — vault path for a summary file
- `summary_filename(source_file: Path) -> str` — `<name>.<ext>.md` filename
- `vault_path_for_source(source_file: Path, codebase_root: Path, vault_root: Path) -> Path` — full Path of the summary file in the vault
- `source_path_from_vault(summary_file: Path, vault_root: Path, codebase_root: Path) -> Path` — reverse: summary file → original source path
- `wikilink(vault_summary_path: Path, vault_root: Path) -> str` — Obsidian wikilink string `[[relative/path/to/file]]`
- `slugify(text: str) -> str` — URL-safe slug for query filenames
- `compute_md5(path: Path) -> str` — 32-character hex MD5 of file contents
- `extract_stored_md5(summary_path: Path) -> str | None` — parses `<!-- md5: ... -->` footer; returns None if absent/malformed
- `md5_footer(hexdigest: str) -> str` — formats the footer comment string

---

#### `EXCLUDED_DIRS`, `BINARY_EXTENSIONS`, `VAULT_SPECIAL_FILES`, and `VAULT_EXCLUDED_DIRS`

```python
EXCLUDED_DIRS: frozenset[str] = frozenset({
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".maps",
})

BINARY_EXTENSIONS: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg",
    ".pdf", ".zip", ".tar", ".gz", ".exe", ".dll", ".so",
    ".pyc", ".class", ".wasm",
})

VAULT_SPECIAL_FILES: frozenset[str] = frozenset({
    "index.md", "log.md", "overview.md", "lint-report.md"
})

VAULT_EXCLUDED_DIRS: frozenset[str] = frozenset({"logs", "queries"})
```

These are module-level constants used by both `vault.py` helpers and many downstream consumers. Defining them all here (not in scanner.py, deletion.py, index_writer.py, or analysis.py) means every consumer gets a single authoritative source and there is no duplication.

`VAULT_SPECIAL_FILES` lists the `.md` files at the vault root that are NOT source-file summaries (they must be excluded from deletion scans, index walks, and backlink scans). `VAULT_EXCLUDED_DIRS` lists the vault directory names that must be pruned when walking the vault for summary files.

---

#### `is_binary_file(path: Path) -> bool`

Returns `True` if the file should be excluded as binary. A file is considered binary when:
1. Its suffix (lowercased) is in `BINARY_EXTENSIONS`, OR
2. It contains null bytes (`\x00`) in its first 8,192 bytes, OR
3. Its first 8,192 bytes cannot be decoded as UTF-8.

Reading only the first 8,192 bytes avoids loading large files into memory just for the binary check.

```python
def is_binary_file(path: Path) -> bool:
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return True
    try:
        chunk = path.read_bytes()[:8192]
        if b"\x00" in chunk:
            return True
        chunk.decode("utf-8")
        return False
    except (OSError, UnicodeDecodeError):
        return True
```

The `except OSError` handles unreadable files — treat them as binary (skip them) rather than crashing.

---

#### `summary_filename(source_file: Path) -> str`

Returns the summary filename for a given source file. The spec mandates that the source extension is always preserved and `.md` is appended as an additional extension:
- `user_service.py` → `user_service.py.md`
- `Makefile` → `Makefile.md`
- `.env` → `.env.md`

```python
def summary_filename(source_file: Path) -> str:
    return source_file.name + ".md"
```

This is intentionally simple: append `.md` to the full filename (including any existing extension). `Path.name` returns the bare filename without parent directories.

---

#### `mirror_path(source_file: Path, codebase_root: Path, vault_root: Path) -> Path`

Computes the vault directory that mirrors the source file's parent directory. Does NOT append the filename — callers use `vault_path_for_source()` for the full summary path.

```python
def mirror_path(source_file: Path, codebase_root: Path, vault_root: Path) -> Path:
    relative_dir = source_file.parent.relative_to(codebase_root)
    return vault_root / relative_dir
```

Both `source_file` and `codebase_root` must be absolute paths (or consistently relative). The function raises `ValueError` (from `relative_to`) if `source_file` is not under `codebase_root` — callers should only pass files discovered under `codebase_root`.

---

#### `vault_path_for_source(source_file: Path, codebase_root: Path, vault_root: Path) -> Path`

Full path of the summary file in the vault.

```python
def vault_path_for_source(
    source_file: Path,
    codebase_root: Path,
    vault_root: Path,
) -> Path:
    return mirror_path(source_file, codebase_root, vault_root) / summary_filename(source_file)
```

Example: `codebase_root=/home/user/myapp`, `source_file=/home/user/myapp/src/auth/login.py`, `vault_root=/home/user/vault`
→ `/home/user/vault/src/auth/login.py.md`

---

#### `source_path_from_vault(summary_file: Path, vault_root: Path, codebase_root: Path) -> Path`

Reverse mapping: given a summary file in the vault, return the corresponding source file path. Used by the scanner (item 5) when enumerating existing summaries to detect deletions.

```python
def source_path_from_vault(
    summary_file: Path,
    vault_root: Path,
    codebase_root: Path,
) -> Path:
    # summary_file.name is like "login.py.md"; strip trailing ".md"
    source_name = summary_file.name[:-3]  # remove ".md"
    relative_dir = summary_file.parent.relative_to(vault_root)
    return codebase_root / relative_dir / source_name
```

Assumes all summary files end in `.md` (the invariant established by `summary_filename()`). The `.name[:-3]` strip removes exactly the `.md` suffix appended by `summary_filename()`.

---

#### `wikilink(vault_summary_path: Path, vault_root: Path) -> str`

Formats an Obsidian wikilink for a summary file. The path is relative to the vault root, and the `.md` extension is omitted (Obsidian convention).

```python
def wikilink(vault_summary_path: Path, vault_root: Path) -> str:
    relative = vault_summary_path.relative_to(vault_root)
    # Remove .md extension: Obsidian wikilinks omit the extension
    without_ext = relative.with_suffix("") if relative.suffix == ".md" else relative
    return f"[[{without_ext.as_posix()}]]"
```

Example: `vault_summary_path=/vault/src/auth/login.py.md`, `vault_root=/vault`
→ `[[src/auth/login.py]]`

Note that `login.py.md` has suffix `.md`, so `with_suffix("")` yields `login.py` — correct. `as_posix()` ensures forward slashes on Windows.

---

#### `slugify(text: str) -> str`

Converts a natural-language query string into a URL-safe filename slug. Per spec: lowercase, spaces → hyphens, strip non-alphanumeric/non-hyphen characters.

```python
import re

def slugify(text: str) -> str:
    slug = text.lower()
    slug = slug.replace(" ", "-")
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug)   # collapse multiple hyphens
    slug = slug.strip("-")               # strip leading/trailing hyphens
    return slug
```

Example: `"How does auth work?"` → `"how-does-auth-work"`

The multi-hyphen collapse and strip handle edge cases like consecutive spaces or leading/trailing punctuation. The spec example confirms the basic conversion; the collapse/strip rules are natural defensive additions that do not contradict the spec.

---

#### `compute_md5(path: Path) -> str`

Computes the MD5 hash of a file's full contents. Returns a 32-character lowercase hexdigest.

```python
import hashlib

def compute_md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
```

Reads in 64 KiB chunks to handle large files without loading them entirely into memory. The spec uses MD5 purely for change detection (not security), so the stdlib `hashlib.md5()` is appropriate with no `usedforsecurity=False` annotation needed (though adding it is harmless for Python 3.9+ where FIPS mode might be active).

---

#### `extract_stored_md5(summary_path: Path) -> str | None`

Reads the last line of a summary file and parses the MD5 footer comment. Returns the hexdigest string if found, or `None` if the file does not exist or has no valid footer.

```python
MD5_FOOTER_RE = re.compile(r"<!--\s*md5:\s*([a-f0-9]{32})\s*-->")

def extract_stored_md5(summary_path: Path) -> str | None:
    if not summary_path.exists():
        return None
    try:
        text = summary_path.read_text(encoding="utf-8")
    except OSError:
        return None
    last_line = text.rstrip("\n").rsplit("\n", 1)[-1]
    m = MD5_FOOTER_RE.search(last_line)
    return m.group(1) if m else None
```

Only the last line is searched — the footer is always the final line of a summary file. Reading the full file is required anyway to get the last line; using `rsplit("\n", 1)` is efficient.

---

#### `md5_footer(hexdigest: str) -> str`

Returns the formatted footer string.

```python
def md5_footer(hexdigest: str) -> str:
    return f"<!-- md5: {hexdigest} -->"
```

---

### `logging_setup.py`

**File**: `codebase_wiki_builder/logging_setup.py`

**Exports**:
- `setup_logging(vault_root: Path) -> logging.Logger` — creates `logs/YYYY-MM-DD_HH-MM-SS.log`, configures root logger, returns logger
- `append_log_md(vault_root: Path, entry: str) -> None` — appends one entry to `log.md`, never truncates

---

#### `setup_logging(vault_root: Path) -> logging.Logger`

Creates the per-run debug log file under `logs/` and configures the Python `logging` module to write all DEBUG-level events to it. Returns the root logger (or a named logger) that the rest of the application uses.

The log filename uses UTC time in `YYYY-MM-DD_HH-MM-SS` format, matching FR-6.2: `logs/2026-04-29_14-30-00.log`.

```python
import logging
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = "logs"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(vault_root: Path) -> logging.Logger:
    logs_dir = vault_root / LOG_DIR
    logs_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    log_file = logs_dir / f"{timestamp}.log"

    logging.basicConfig(
        level=logging.DEBUG,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
        handlers=[logging.FileHandler(log_file, encoding="utf-8")],
        force=True,  # override any existing root logger handlers
    )

    logger = logging.getLogger("codebase_wiki_builder")
    logger.info("Logging initialized. Log file: %s", log_file)
    return logger
```

`force=True` in `logging.basicConfig()` ensures that if the function is called more than once (e.g., in tests), the existing handlers are replaced rather than accumulated. `logs_dir.mkdir(parents=True, exist_ok=True)` creates the `logs/` directory on first run.

The returned logger (`logging.getLogger("codebase_wiki_builder")`) is the application-wide logger. Sub-modules should get child loggers via `logging.getLogger("codebase_wiki_builder.<module>")` — these automatically inherit the root configuration.

---

#### `append_log_md(vault_root: Path, entry: str) -> None`

Appends one timestamped entry to `log.md` in the vault root. Never truncates or overwrites. The file is opened in append mode (`"a"`) on every call — no file handle is held open between calls, which keeps the implementation simple and crash-safe.

The spec mandates UTC timestamps in `YYYY-MM-DD HH:MM:SS UTC` format. The `entry` parameter may or may not already contain a timestamp prefix — callers are responsible for formatting the entry string. `append_log_md` adds a trailing newline.

```python
LOG_MD_FILENAME = "log.md"


def append_log_md(vault_root: Path, entry: str) -> None:
    log_path = vault_root / LOG_MD_FILENAME
    with log_path.open("a", encoding="utf-8") as f:
        f.write(entry.rstrip("\n") + "\n")
```

Callers construct the full entry string including the UTC timestamp prefix. Example caller pattern:

```python
from datetime import datetime, timezone

def _log_entry(operation: str, detail: str) -> str:
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"{ts} | {operation} | {detail}"

append_log_md(vault_root, _log_entry("ingest", "files=10 summarized=8 skipped=2"))
```

This keeps `append_log_md` minimal (just the write) and puts timestamp formatting in callers, which know what format is appropriate for their log entry type.

---

## Error Handling

| Condition | Location | Behavior |
|-----------|----------|----------|
| `source_file` not under `codebase_root` | `mirror_path()` | Raises `ValueError` from `Path.relative_to()` — callers must only pass files discovered under `codebase_root` |
| Unreadable file in `is_binary_file()` | `is_binary_file()` | `OSError` caught → returns `True` (treat as binary/skip) |
| `summary_path` does not exist | `extract_stored_md5()` | Returns `None` — caller treats file as new (no stored hash) |
| Unreadable summary in `extract_stored_md5()` | `extract_stored_md5()` | `OSError` caught → returns `None` (same as no stored hash) |
| `logs/` directory cannot be created | `setup_logging()` | `OSError` propagates — fatal, cannot proceed without log directory |
| `log.md` not writable | `append_log_md()` | `OSError` propagates — callers may catch and log to debug logger instead |

---

## Unit Test Specifications

**File**: `tests/test_vault.py` and `tests/test_logging_setup.py`

---

### `vault.py` — `is_binary_file()`

| Case | Input | Expected | Why |
|------|-------|----------|-----|
| Known binary extension | `.png` file | `True` | Extension match |
| `.pyc` extension | `.pyc` file | `True` | Extension match |
| Plain text file | File with ASCII content | `False` | Not binary |
| File with null byte | File containing `\x00` | `True` | Null byte detection |
| Non-UTF-8 bytes | File with `\xff\xfe` (not UTF-8) | `True` | Decode failure |
| Empty file | Zero-byte file | `False` | Empty = valid UTF-8 |
| Unreadable file | File with no read permission | `True` | OSError → treat as binary |

---

### `vault.py` — `summary_filename()`

| Case | Input | Expected | Why |
|------|-------|----------|-----|
| Python file | `Path("user_service.py")` | `"user_service.py.md"` | Extension preserved, `.md` appended |
| No extension (Makefile) | `Path("Makefile")` | `"Makefile.md"` | `.md` appended to bare name |
| Hidden file | `Path(".env")` | `".env.md"` | Dotfile handled |
| Already `.md` file | `Path("README.md")` | `"README.md.md"` | Spec: always append `.md` regardless of existing extension |
| Nested path | `Path("src/auth/login.py")` | `"login.py.md"` | Uses `.name` only |

---

### `vault.py` — `vault_path_for_source()` and `mirror_path()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Simple nested file | `codebase=/cb`, `source=/cb/src/auth/login.py`, `vault=/vault` | `/vault/src/auth/login.py.md` | Full mirror with naming |
| File at codebase root | `source=/cb/main.py` | `/vault/main.py.md` | Root-level files |
| Deep nesting | `source=/cb/a/b/c/d.py` | `/vault/a/b/c/d.py.md` | Arbitrary depth |

---

### `vault.py` — `source_path_from_vault()` (round-trip)

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Round-trip: source → vault → source | Any valid source path | Original source path recovered | Inverse of `vault_path_for_source` |
| `.py.md` → `.py` | `/vault/src/foo.py.md` | `/cb/src/foo.py` | Strips only trailing `.md` |

---

### `vault.py` — `wikilink()`

| Case | Input | Expected | Why |
|------|-------|----------|-----|
| Nested summary | `/vault/src/auth/login.py.md`, vault=`/vault` | `[[src/auth/login.py]]` | Relative path, no `.md` extension |
| Root-level summary | `/vault/main.py.md` | `[[main.py]]` | Root file |
| Uses forward slashes | Any path (even on Windows) | Path uses `/` not `\` | `as_posix()` |

---

### `vault.py` — `slugify()`

| Case | Input | Expected | Why |
|------|-------|----------|-----|
| Basic question | `"How does auth work?"` | `"how-does-auth-work"` | Spec example |
| Multiple spaces | `"foo  bar"` | `"foo-bar"` | Multi-hyphen collapsed |
| Leading/trailing punct | `"?hello world!"` | `"hello-world"` | Strip non-alphanumeric at edges |
| Already clean | `"simple-slug"` | `"simple-slug"` | No change |
| All punctuation | `"???"` | `""` | Empty slug (callers must handle) |
| Mixed case | `"How Does AUTH Work"` | `"how-does-auth-work"` | Lowercased |

---

### `vault.py` — `compute_md5()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Known content | File with `b"hello"` | `md5(b"hello").hexdigest()` = `"5d41402abc4b2a76b9719d911017c592"` | Correct hash |
| Empty file | Zero-byte file | `md5(b"").hexdigest()` = `"d41d8cd98f00b204e9800998ecf8427e"` | Edge case |
| Returns 32 chars | Any file | `len(result) == 32` and all hex chars | Format requirement |

---

### `vault.py` — `extract_stored_md5()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Valid footer | File ending `<!-- md5: abc123...{32} -->` | Returns 32-char hexdigest | Happy path |
| Missing footer | File with no MD5 comment | `None` | Not yet hashed |
| Malformed footer | `<!-- md5: tooshort -->` | `None` | Regex requires 32 hex chars |
| Nonexistent file | Path does not exist | `None` | New file, no summary yet |
| Footer not on last line | MD5 comment in middle | `None` | Only last line searched |

---

### `vault.py` — `md5_footer()`

| Case | Input | Expected | Why |
|------|-------|----------|-----|
| Valid hexdigest | `"abc"` | `"<!-- md5: abc -->"` | Correct format |

---

### `logging_setup.py` — `setup_logging()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Creates `logs/` dir | Call with fresh tmp dir | `logs/` subdirectory created | FR-6.2: directory must exist |
| Log file created | Call once | One `.log` file exists under `logs/` | FR-6.2 |
| Filename format | Check filename | Matches `\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.log` | FR-6.2: `YYYY-MM-DD_HH-MM-SS` format |
| Returns logger | Call | Returns instance of `logging.Logger` | Callers use the returned logger |
| Writes DEBUG events | Call; `logger.debug("test")` | Log file contains "test" | DEBUG level captured |

---

### `logging_setup.py` — `append_log_md()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Creates file if absent | Fresh vault dir; call once | `log.md` created with one line | FR-6.1: file created on first use |
| Appends, never truncates | Call twice with different entries | `log.md` has both entries | FR-6.1: append-only invariant |
| Trailing newline | Call with any entry | Entry ends with `\n` in file | Clean line formatting |
| Strips trailing newline from entry | Entry string already ends with `\n` | Only one `\n` at end of line | `rstrip("\n") + "\n"` pattern |
| Existing file preserved | Pre-populate `log.md` with content; call | Original content still present at top | Never truncated |

**Key Scenario: Append-only invariant**

```python
def test_append_log_md_never_truncates(tmp_path):
    from codebase_wiki_builder.logging_setup import append_log_md

    first_entry = "2026-04-29 10:00:00 UTC | ingest | files=5"
    second_entry = "2026-04-29 10:01:00 UTC | ingest | files=3"

    append_log_md(tmp_path, first_entry)
    append_log_md(tmp_path, second_entry)

    log_path = tmp_path / "log.md"
    content = log_path.read_text(encoding="utf-8")
    assert first_entry in content
    assert second_entry in content
    # Ensure first entry comes before second
    assert content.index(first_entry) < content.index(second_entry)
```

**Key Scenario: Binary detection with null byte**

```python
def test_is_binary_null_byte(tmp_path):
    from codebase_wiki_builder.vault import is_binary_file

    f = tmp_path / "data.bin"
    f.write_bytes(b"some text\x00more text")
    assert is_binary_file(f) is True
```

---

## Notes

- **`vault.py` has no side effects at import time**: All functions are pure or read-only filesystem operations. No module-level I/O, no global state mutation. This makes it safe to import in tests without setUp/tearDown concerns.
- **`logging_setup.py` uses UTC for the debug log filename**: `datetime.now(tz=timezone.utc)` ensures the filename timestamp matches the UTC timestamps in `log.md` entries. Both sinks use UTC throughout.
- **`append_log_md()` does not add a timestamp**: Callers supply the full entry string. This keeps the function single-responsibility (write, don't format). The helper pattern shown in the implementation details (a `_log_entry()` function in each caller module) is the recommended approach.
- **`setup_logging()` uses `force=True`**: This is essential for test isolation — without it, calling `setup_logging()` multiple times in a test suite accumulates handlers and causes duplicate log output. `force=True` replaces existing handlers.
- **`logs/` directory is not auto-rotated**: The spec explicitly states old log files are not deleted by the application. The `logs/` directory will grow unbounded; this is intentional and documented.
- **`is_binary_file()` reads only 8,192 bytes for detection**: This is sufficient for the null-byte and UTF-8 decode checks. The spec does not specify a chunk size; 8 KiB is a conventional choice that balances accuracy and performance.
- **`wikilink()` uses `as_posix()`**: Obsidian wikilinks always use forward slashes regardless of OS. `Path.as_posix()` ensures this invariant on Windows.
- **`slugify()` may return an empty string**: If the entire input consists of non-alphanumeric characters, the result is `""`. Callers (item 12, query persistence) must handle this edge case by falling back to a default slug (e.g., `"query"`).
- **`extract_stored_md5()` only searches the last line**: The spec places the MD5 footer as the final line of every summary file. Searching only the last line is both correct and efficient. If a file's last line is empty (trailing newline), `rsplit("\n", 1)` correctly returns the line before it.
- **No dependency on `config.py`**: `vault.py` and `logging_setup.py` depend only on stdlib. They do not import `WikiConfig` — they accept raw `Path` arguments. This keeps the utility layer decoupled from the config layer and avoids circular imports.
