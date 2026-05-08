# Implementation Plan: Extract write_query_log_entry Helper

## Spec Context

This plan implements FR-QC-7a from the query-cache specification. The current `_write_log_entry()`
private function in `query_persistence.py` handles log writes for fresh query saves only. This
plan extracts it into a public `write_query_log_entry()` helper that also supports cache-hit log
entries, enabling both `save_query_page()` and the future MCP cache-hit path to call a single
shared log-write function without duplicating logic.

Catalog item: Extract write_query_log_entry Helper
Specification section: FR-QC-7a; also referenced in FR-QC-6 (CLI cache hit) and FR-QC-7 (MCP cache hit)
Acceptance criteria addressed: AT-1 (log entry still appended on cache hit), and the explicit
constraint "Do NOT skip the `log.md` entry for cache hits."

---

## Dependencies

- **Blocked by**: Nothing — this plan modifies only `query_persistence.py` (and its new test file).
  It has no runtime dependency on `query_cache.py` or any plan that doesn't yet exist.
- **Blocks**: The MCP cache-hit integration plan (which calls `write_query_log_entry()` with
  `cache_hit=True, cached_path=...`) and the CLI cache-hit plan (which calls it with `cache_hit=True`).
- **Uses**: `Callable` (already imported from `typing`); `Path` (already imported); `_utc_now()`
  (already defined in same file).

---

## File Changes

### New Files

- `tests/test_query_persistence.py` — New test file for `query_persistence` module (does not
  currently exist; confirmed via `find`).

### Modified Files

- `codebase_wiki_builder/query_persistence.py` — Add public `write_query_log_entry()` function;
  update `save_query_page()` to call it; remove `_write_log_entry()`.

---

## Implementation Details

### New public helper — `write_query_log_entry()`

**File**: `codebase_wiki_builder/query_persistence.py`

**Location**: Add in the "Internal helpers — log entry" section, replacing `_write_log_entry()`.
Move the new function above the `# Internal helpers — parsing` block, keeping it in the same
logical position.

**Signature**:

```python
def write_query_log_entry(
    question: str,
    vault_root: Path,
    log_fn: Callable[[str], None],
    cache_hit: bool = False,
    cached_path: str | None = None,
) -> None:
```

**Parameters**:
- `question` — the user's original question string
- `vault_root` — absolute path to the vault root (used only for the fresh-query path; unused on
  cache hits but kept for a uniform call signature)
- `log_fn` — callable that accepts a pre-formatted log entry string and appends it to `log.md`
- `cache_hit` — `False` (default) for fresh saves; `True` for cache-hit log entries
- `cached_path` — vault-relative path string of the matched cached page (e.g.
  `"queries/how-does-auth-work.md"`); used only when `cache_hit=True`; `None` otherwise

**Log entry formats**:

- Fresh query (`cache_hit=False`):
  ```
  {timestamp} | query-saved | {question} → {rel_path}
  ```
  Where `rel_path` is computed from `vault_root` at call time via `_utc_now()` for `timestamp`.
  Wait — on a fresh query `save_query_page()` already has both the timestamp and the page path.
  To avoid computing a second timestamp, `write_query_log_entry()` calls `_utc_now()` internally
  for its own timestamp on the cache-hit path. For the fresh path, `save_query_page()` must pass
  the page path some other way.

  **Resolution**: `write_query_log_entry()` takes a `page_path: Path | None = None` parameter for
  fresh queries (see Revised Signature below). The fresh-query format uses the `page_path`
  relative to `vault_root`.

**Revised Signature** (incorporating all parameters):

```python
def write_query_log_entry(
    question: str,
    vault_root: Path,
    log_fn: Callable[[str], None],
    cache_hit: bool = False,
    cached_path: str | None = None,
    page_path: Path | None = None,
) -> None:
```

However, re-reading the spec task description, the target signature is:

```python
def write_query_log_entry(
    question: str,
    vault_root: Path,
    log_fn: Callable[[str], None],
    cache_hit: bool = False,
    cached_path: str | None = None,
) -> None:
```

The task description does not include `page_path`. For a fresh query (`cache_hit=False`),
`save_query_page()` owns the `page_path` and `timestamp`. The cleanest approach that matches the
spec signature exactly is:

- On `cache_hit=False`: the caller (`save_query_page()`) still computes the relative path string
  and passes it through `cached_path` (which carries the page path even for fresh queries).
  **But that conflates two separate concepts.** Better:

- **Preferred approach**: keep the parameter name generic — rename `cached_path` to `page_path`
  in the implementation — but the spec says `cached_path`. So use `cached_path` for both
  directions and treat it as "the relevant path" regardless of `cache_hit`:

  - Fresh query: `save_query_page()` passes
    `cached_path=page_path.relative_to(vault_root).as_posix()` and `cache_hit=False`.
  - Cache hit: callers pass `cached_path="queries/how-does-auth-work.md"` and `cache_hit=True`.

This matches the spec signature exactly. `cached_path` is `str | None`; on fresh queries the
caller provides it as the newly saved page's vault-relative path.

**Final Signature** (matches spec, used in implementation):

```python
def write_query_log_entry(
    question: str,
    vault_root: Path,
    log_fn: Callable[[str], None],
    cache_hit: bool = False,
    cached_path: str | None = None,
) -> None:
    """Write a query log entry to log.md via the provided log_fn callable.

    On cache_hit=False (fresh query):
        Format: {timestamp} | query-saved | {question} → {cached_path}
        where cached_path is the vault-relative path of the newly saved page.

    On cache_hit=True:
        Format: {timestamp} | cache-hit | {question} | {cached_path}
        where cached_path is the vault-relative path of the matched cached page.

    Args:
        question:    The user's original question.
        vault_root:  Absolute path to the vault root (unused at runtime; reserved for
                     future use and signature symmetry with save_query_page()).
        log_fn:      Callable that accepts a pre-formatted log entry string.
        cache_hit:   False for fresh saves; True for cache-hit entries.
        cached_path: Vault-relative path string of the relevant page.
                     - Fresh: the newly saved page (e.g. "queries/how-does-auth-work.md").
                     - Cache hit: the matched existing page.
                     May be None if the path is unavailable; the entry is still written.
    """
    timestamp = _utc_now()
    path_str = cached_path or ""
    if cache_hit:
        log_fn(f"{timestamp} | cache-hit | {question} | {path_str}")
    else:
        log_fn(f"{timestamp} | query-saved | {question} → {path_str}")
```

**Note on `vault_root`**: The parameter is kept in the signature for call-site symmetry (all
callers already hold a `vault_root`), but `write_query_log_entry()` does not use it at runtime.
`_utc_now()` is called internally to generate the timestamp. This means the log timestamp for
fresh queries will be captured inside `write_query_log_entry()` rather than passed from
`save_query_page()`. This is a minor change in behavior (previously `save_query_page()` used one
timestamp for both the page file and the log entry; now the log entry timestamp is captured
slightly later). The difference is at most a few milliseconds and does not affect correctness.

---

### Update `save_query_page()`

**File**: `codebase_wiki_builder/query_persistence.py`

Replace the call to `_write_log_entry(question, page_path, vault_root, timestamp, log_fn)` at
line 123 with a call to `write_query_log_entry()`:

```python
# Before (remove):
_write_log_entry(question, page_path, vault_root, timestamp, log_fn)

# After (add):
write_query_log_entry(
    question=question,
    vault_root=vault_root,
    log_fn=log_fn,
    cache_hit=False,
    cached_path=page_path.relative_to(vault_root).as_posix(),
)
```

The `timestamp` local variable is still used earlier in `save_query_page()` (for
`_build_page_content(question, result, timestamp)`), so it is NOT removed.

---

### Remove `_write_log_entry()`

**File**: `codebase_wiki_builder/query_persistence.py`

Delete the entire `_write_log_entry()` private function (lines 279–288 in the current file):

```python
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
```

After removal, verify no other call site references `_write_log_entry`.

---

### Module docstring update

**File**: `codebase_wiki_builder/query_persistence.py`

Update the `Public API:` block in the module-level docstring to include the new helper:

```
Public API:
  - QueryPage: dataclass representing a parsed saved query page (for lint use)
  - save_query_page(): persist a query result to queries/<slug>.md
  - read_query_page(): parse an existing saved query page into a QueryPage
  - write_query_log_entry(): write a query log entry to log.md (fresh or cache-hit)
```

---

## Error Handling

- If `cached_path` is `None`, `path_str` falls back to `""`. The log entry is still written with
  an empty path segment. This prevents a crash while still recording the event.
- `write_query_log_entry()` does not catch exceptions from `log_fn`. If `log_fn` raises (e.g.,
  file-system error), the exception propagates to the caller — consistent with the behavior of
  the old `_write_log_entry()`.

---

## Unit Test Specifications

**File**: `tests/test_query_persistence.py` (new file)

### Test class structure

```
TestWriteQueryLogEntry
  test_fresh_query_log_format
  test_fresh_query_uses_query_saved_marker
  test_cache_hit_log_format
  test_cache_hit_uses_cache_hit_marker
  test_cache_hit_with_none_cached_path
  test_fresh_query_with_none_cached_path
  test_timestamp_is_utc_formatted
  test_log_fn_called_exactly_once

TestSaveQueryPage
  test_calls_write_query_log_entry (integration: log_fn is called with correct content)
  test_does_not_call_removed_write_log_entry (smoke: _write_log_entry no longer exists)
```

### Detailed test cases

#### `TestWriteQueryLogEntry`

| Case | `cache_hit` | `cached_path` | Expected log entry pattern |
|------|-------------|---------------|---------------------------|
| Fresh query | `False` | `"queries/auth.md"` | `"... \| query-saved \| How does auth work? → queries/auth.md"` |
| Cache hit | `True` | `"queries/auth.md"` | `"... \| cache-hit \| How does auth work? \| queries/auth.md"` |
| Cache hit, no path | `True` | `None` | `"... \| cache-hit \| How does auth work? \| "` |
| Fresh, no path | `False` | `None` | `"... \| query-saved \| How does auth work? → "` |

**Key scenario: fresh query log format**
```python
def test_fresh_query_log_format(self, tmp_path):
    from codebase_wiki_builder.query_persistence import write_query_log_entry
    entries = []
    write_query_log_entry(
        question="How does auth work?",
        vault_root=tmp_path,
        log_fn=entries.append,
        cache_hit=False,
        cached_path="queries/how-does-auth-work.md",
    )
    assert len(entries) == 1
    entry = entries[0]
    assert "| query-saved |" in entry
    assert "How does auth work?" in entry
    assert "queries/how-does-auth-work.md" in entry
    assert "→" in entry
```

**Key scenario: cache-hit log format**
```python
def test_cache_hit_log_format(self, tmp_path):
    from codebase_wiki_builder.query_persistence import write_query_log_entry
    entries = []
    write_query_log_entry(
        question="How does auth work?",
        vault_root=tmp_path,
        log_fn=entries.append,
        cache_hit=True,
        cached_path="queries/how-does-auth-work.md",
    )
    assert len(entries) == 1
    entry = entries[0]
    assert "| cache-hit |" in entry
    assert "How does auth work?" in entry
    assert "queries/how-does-auth-work.md" in entry
    # The pipe separator (not arrow) is used for cache-hit entries
    assert "→" not in entry
```

**Key scenario: log_fn called exactly once**
```python
def test_log_fn_called_exactly_once(self, tmp_path):
    from codebase_wiki_builder.query_persistence import write_query_log_entry
    from unittest.mock import MagicMock
    mock_log = MagicMock()
    write_query_log_entry(
        question="Q?",
        vault_root=tmp_path,
        log_fn=mock_log,
        cache_hit=False,
        cached_path="queries/q.md",
    )
    mock_log.assert_called_once()
```

**Key scenario: timestamp is UTC-formatted**
```python
def test_timestamp_is_utc_formatted(self, tmp_path):
    from codebase_wiki_builder.query_persistence import write_query_log_entry
    import re
    entries = []
    write_query_log_entry(
        question="Q?",
        vault_root=tmp_path,
        log_fn=entries.append,
    )
    # Timestamp prefix format: "YYYY-MM-DD HH:MM:SS UTC"
    assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC", entries[0])
```

#### `TestSaveQueryPage`

**Key scenario: log_fn receives correct content after refactor**
```python
def test_log_fn_called_with_query_saved_entry(self, tmp_path):
    """save_query_page() delegates log writes to write_query_log_entry() correctly."""
    from codebase_wiki_builder.query_persistence import save_query_page
    from codebase_wiki_builder.query_engine import QueryResult
    # Create minimal vault structure
    (tmp_path / "index.md").write_text("| File | Description |\n|------|-------------|\n")
    entries = []
    result = QueryResult(
        answer="The answer.\n\n## Sources\n- src/auth.py.md\n",
        sources=["src/auth.py.md"],
        one_line_summary="Explains auth.",
    )
    save_query_page(
        question="How does auth work?",
        result=result,
        vault_root=tmp_path,
        log_fn=entries.append,
    )
    assert len(entries) == 1
    assert "query-saved" in entries[0]
    assert "How does auth work?" in entries[0]
```

**Key scenario: `_write_log_entry` no longer exists**
```python
def test_write_log_entry_removed(self):
    """The old private helper should no longer exist after this refactor."""
    import codebase_wiki_builder.query_persistence as mod
    assert not hasattr(mod, "_write_log_entry"), (
        "_write_log_entry should have been removed; use write_query_log_entry instead"
    )
```

---

## Notes

1. **`vault_root` is kept but unused**: The `vault_root` parameter is part of the spec-mandated
   signature. It is included for call-site symmetry and forward-compatibility (e.g., a future
   path-relativization step could use it). Passing it does not affect behavior.

2. **Separator difference between fresh and cache-hit entries**: Fresh query entries use `→`
   (right arrow, U+2192) as the separator between question and path (matching the existing
   `_write_log_entry()` format). Cache-hit entries use `|` (pipe) as the separator between all
   fields (matching the `{timestamp} | cache-hit | {question} | {cached_path}` format from the
   spec). The two formats are intentionally different.

3. **Timestamp is generated inside `write_query_log_entry()`**: This means the log entry
   timestamp will be captured at log-write time, not at page-save time. The gap is at most a few
   milliseconds. This is the correct behavior for cache-hit entries (which have no "save" time)
   and acceptable for fresh entries.

4. **`_write_log_entry()` removal is safe**: The only call site is inside `save_query_page()`
   (line 123), which this plan replaces. No other module imports or calls `_write_log_entry()`
   (it is private and not exported). Confirmed by reading `query_persistence.py` in full.

5. **`Callable` import is already present**: `query_persistence.py` already imports
   `from typing import TYPE_CHECKING, Callable`. No new imports are needed.

6. **`from __future__ import annotations` is already present**: `str | None` in the function
   signature is safe without any additional imports.

7. **`write_query_log_entry` must appear in module `Public API` docstring**: Update the
   module-level docstring so downstream developers know this function is part of the public API
   and safe to import directly (the MCP cache-hit plan will do exactly that).
