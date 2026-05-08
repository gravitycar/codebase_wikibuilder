# Implementation Plan: Promote parse_existing_index to Public API

## Spec Context

This plan promotes `_parse_existing_index()` from a private helper to a public
function by removing the leading underscore. No logic changes are involved — this
is a pure rename across two files. The function reads an existing `index.md` and
returns a `dict[str, str]` mapping wikilink targets to their description strings;
making it public allows external callers (e.g. a future cache layer) to reuse this
parsing logic without going through `rebuild_index()`.

Catalog item: Promote `parse_existing_index` to Public API
Specification section: Index Writer public API surface
Acceptance criteria addressed:
- `parse_existing_index` is importable directly from `codebase_wiki_builder.index_writer`
- `_parse_existing_index` no longer exists in the module
- All internal call sites updated
- All test call sites updated

## Dependencies

- **Blocked by**: None
- **Uses**: `codebase_wiki_builder/index_writer.py` (existing), `tests/test_index_writer.py` (existing)

## File Changes

### New Files

None.

### Modified Files

- `codebase_wiki_builder/index_writer.py` — Rename function definition and update
  the one internal call site
- `tests/test_index_writer.py` — Update import and three call sites

---

## Implementation Details

### 1. `codebase_wiki_builder/index_writer.py`

**Change 1 — function definition (line 75)**

Rename the `def _parse_existing_index(...)` definition to `def parse_existing_index(...)`.
The signature, docstring, and body remain identical.

Before:
```python
def _parse_existing_index(vault_root: Path) -> dict[str, str]:
```

After:
```python
def parse_existing_index(vault_root: Path) -> dict[str, str]:
```

**Change 2 — internal call site in `rebuild_index()` (line 40)**

Update the call to use the new public name.

Before:
```python
old_descriptions = _parse_existing_index(vault_root)
```

After:
```python
old_descriptions = parse_existing_index(vault_root)
```

### 2. `tests/test_index_writer.py`

**Change 1 — import block (line 16)**

The import currently lists `_parse_existing_index`. Rename it to `parse_existing_index`.

Before:
```python
from codebase_wiki_builder.index_writer import (
    rebuild_index,
    _collect_query_pages,
    _collect_summary_pages,
    _extract_description,
    _overview_description,
    _parse_existing_index,
    _write_index,
)
```

After:
```python
from codebase_wiki_builder.index_writer import (
    rebuild_index,
    _collect_query_pages,
    _collect_summary_pages,
    _extract_description,
    _overview_description,
    parse_existing_index,
    _write_index,
)
```

**Change 2 — call site on line 127**

Before:
```python
result = _parse_existing_index(vault)
```

After:
```python
result = parse_existing_index(vault)
```

**Change 3 — call site on line 139**

Before:
```python
result = _parse_existing_index(vault)
```

After:
```python
result = parse_existing_index(vault)
```

**Change 4 — call site on line 149**

Before:
```python
result = _parse_existing_index(vault)
```

After:
```python
result = parse_existing_index(vault)
```

---

## Error Handling

No new error handling needed. The function body is unchanged; only the name changes.

---

## Unit Test Specifications

No new test cases are required. The three existing call sites in `TestParseExistingIndex`
already cover the function's behaviour. The rename itself is validated by the fact that
the tests import and call `parse_existing_index` (formerly `_parse_existing_index`) — if
the rename is incomplete, the import will raise `ImportError` and every test in the suite
will fail immediately, giving clear signal that the change is incomplete.

| Existing test | Validates after rename |
|---|---|
| `test_returns_empty_when_no_index` | Function callable as `parse_existing_index`; returns `{}` when no file |
| `test_parses_description` | Parses description cell from table row |
| `test_parses_stale_annotation` | Preserves stale annotation in description |

---

## Notes

- The other private helpers (`_collect_query_pages`, `_collect_summary_pages`,
  `_extract_description`, `_overview_description`, `_write_index`) keep their
  leading underscore; only `parse_existing_index` is promoted.
- No `__all__` list exists in `index_writer.py`, so no module-level export list
  needs updating.
- No other files in the project import `_parse_existing_index` — confirm with a
  quick grep before applying the change.
