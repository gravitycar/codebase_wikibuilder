# Implementation Plan: Promote has_stale_banner to Public API

## Spec Context

This plan fulfills the requirement to expose `has_stale_banner()` as part of the public API of
`codebase_wiki_builder.staleness`. The function currently exists as a private helper
(`_has_stale_banner`). Promoting it removes the leading underscore, making it importable by
external callers (e.g., the query-cache feature that needs to check for stale banners before
deciding whether an answer is still valid).

Catalog item: Promote `has_stale_banner` to Public API
Specification section: Public API surface for staleness detection
Acceptance criteria addressed:
- `has_stale_banner(content)` is importable from `codebase_wiki_builder.staleness` without underscore prefix
- No change to function logic or signature
- All existing tests continue to pass (updated to reference new name)

## Dependencies

- **Blocked by**: nothing — this is a pure rename with no logic changes
- **Uses**: existing `_STALE_BANNER_RE` compiled regex in `staleness.py` (unchanged)

## File Changes

### New Files

None.

### Modified Files

- `codebase_wiki_builder/staleness.py` — rename function definition; update one internal call site
- `tests/test_staleness.py` — update import and all call sites to use new name

## Implementation Details

### 1. Rename the function definition

**File**: `codebase_wiki_builder/staleness.py`

**Line 181** — change:

```python
def _has_stale_banner(content: str) -> bool:
    """Return True if the content already contains a stale callout banner."""
    return bool(_STALE_BANNER_RE.search(content))
```

to:

```python
def has_stale_banner(content: str) -> bool:
    """Return True if the content already contains a stale callout banner."""
    return bool(_STALE_BANNER_RE.search(content))
```

No change to the docstring, body, or signature beyond the name.

### 2. Update the internal call site

**File**: `codebase_wiki_builder/staleness.py`

**Line 342** — change:

```python
    if _has_stale_banner(content):
```

to:

```python
    if has_stale_banner(content):
```

### 3. Update the test import

**File**: `tests/test_staleness.py`

**Lines 12–19** — change:

```python
from codebase_wiki_builder.staleness import (
    StalenessResult,
    _has_stale_banner,
    _insert_stale_banner,
    _annotate_index_row,
    _parse_sources_section,
    detect_stale_queries,
)
```

to:

```python
from codebase_wiki_builder.staleness import (
    StalenessResult,
    has_stale_banner,
    _insert_stale_banner,
    _annotate_index_row,
    _parse_sources_section,
    detect_stale_queries,
)
```

### 4. Update all test call sites

**File**: `tests/test_staleness.py`

All four locations use `_has_stale_banner`; replace each with `has_stale_banner`:

| Line | Old | New |
|------|-----|-----|
| 80 (comment) | `# _has_stale_banner` | `# has_stale_banner` |
| 86 | `assert _has_stale_banner(content) is False` | `assert has_stale_banner(content) is False` |
| 90 | `assert _has_stale_banner(content) is True` | `assert has_stale_banner(content) is True` |
| 96 | `assert _has_stale_banner(content) is False` | `assert has_stale_banner(content) is False` |

Line 80 is the comment immediately before the `TestHasStaleBanner` class body; update it for
consistency but it has no runtime effect.

## Error Handling

No error-handling changes — this is a pure rename.

## Unit Test Specifications

No new test cases are needed. The existing `TestHasStaleBanner` class in `tests/test_staleness.py`
covers all required scenarios and will continue to pass once the name is updated.

| Test | Expected after rename |
|------|-----------------------|
| `test_no_banner` | `has_stale_banner(content) is False` — passes unchanged |
| `test_detects_stale_banner` | `has_stale_banner(content) is True` — passes unchanged |
| `test_case_sensitive` | `has_stale_banner(content) is False` — passes unchanged |

## Notes

- There is no `__all__` list in `staleness.py`, so no additional export declaration is needed.
- No other files in the codebase reference `_has_stale_banner` (confirmed by grep: only lines 181
  and 342 in `staleness.py`, and lines 14, 86, 90, 96 in `test_staleness.py`).
- The rename is purely cosmetic at the Python level; callers that previously imported the private
  name will receive an `ImportError` after this change (intended — the old private name should not
  be used externally).
