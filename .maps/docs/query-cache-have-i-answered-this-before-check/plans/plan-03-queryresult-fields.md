# Implementation Plan: Extend QueryResult with Cache Fields

## Spec Context

This plan implements FR-QC-4 from the query-cache specification. `QueryResult` is the
dataclass returned by `run_query()` and consumed by both the CLI and MCP callers. Adding
`from_cache`, `cached_path`, and `cached_at` as optional fields (with "not a cache hit"
defaults) gives downstream callers a single authoritative signal for cache status without
breaking any existing call site.

Catalog item: Extend QueryResult with Cache Fields
Specification section: FR-QC-4, Data Requirements — QueryResult Schema Change
Acceptance criteria addressed: AT-10 (from_cache default is False), and the backward-compatibility
requirement that existing code constructing QueryResult without these fields is unaffected.

---

## Dependencies

- **Blocked by**: Nothing — this plan modifies only `query_engine.py` and its test file. It has no
  runtime dependency on the cache module (`query_cache.py`) or any other plan.
- **Blocks**: plan-04 (query_cache module) — `check_query_cache()` constructs `QueryResult` with
  `from_cache=True`, `cached_path=...`, `cached_at=...`, so it must be written after this plan is
  built.
- **Uses**: `dataclasses.field` and `pathlib.Path` (already imported in `query_engine.py`).

---

## File Changes

### New Files

None.

### Modified Files

- `codebase_wiki_builder/query_engine.py` — Add three new optional fields to `QueryResult`.
- `tests/test_query_engine.py` — Add a test class for the new fields; confirm existing assertions
  still pass.

---

## Implementation Details

### QueryResult dataclass — `codebase_wiki_builder/query_engine.py`

**File**: `codebase_wiki_builder/query_engine.py`

**Current definition** (lines 73–88):

```python
@dataclass
class QueryResult:
    """Result of a successful run_query() call."""

    answer: str
    sources: list[str]
    one_line_summary: str
    stale_warnings: list[str] = field(default_factory=list)
```

**New definition** — append three fields after `stale_warnings`, keeping all existing fields and
their order unchanged:

```python
@dataclass
class QueryResult:
    """Result of a successful run_query() call."""

    answer: str
    """The full answer text including the ## Sources section."""

    sources: list[str]
    """Vault-relative paths of included summary files (e.g. ["src/auth/login.py.md"])."""

    one_line_summary: str
    """LLM-generated one-line description for index.md."""

    stale_warnings: list[str] = field(default_factory=list)
    """Vault-relative paths of query pages currently flagged as stale. Empty list if none."""

    from_cache: bool = False
    """True if the result was returned from a saved page without running LLM calls."""

    cached_path: Path | None = None
    """Vault-relative path of the matched cache page (e.g. Path("queries/how-does-auth-work.md")).
    None on fresh (non-cached) results."""

    cached_at: str | None = None
    """The saved_at timestamp string from the matched page's ## Page Metadata section.
    None on fresh results, or when saved_at is absent/unparseable in the cached page."""
```

**Key constraints**:
- All three new fields have defaults, so they appear after `stale_warnings` (which also has a
  default). The dataclass field ordering rule (fields with defaults must follow fields without
  defaults) is already satisfied — `answer`, `sources`, `one_line_summary` have no defaults;
  `stale_warnings`, `from_cache`, `cached_path`, `cached_at` all have defaults.
- `Path | None` uses the union shorthand available in Python 3.10+. The file already uses
  `from __future__ import annotations`, so this syntax is safe at runtime on Python 3.9 as well
  (annotations are treated as strings). The project targets Python 3.12+, so this is doubly safe.
- No `field(default=...)` wrapper is needed for `False` or `None` — bare literal defaults are fine
  for immutable values.

**Import requirements**: `Path` is already imported (`from pathlib import Path`). No new imports.

---

## Error Handling

No error handling changes. The new fields are simple value fields with defaults; there is no
validation logic at construction time.

---

## Unit Test Specifications

**File**: `tests/test_query_engine.py`

Add a new test class `TestQueryResultCacheFields` after the existing `TestQueryContextWindow` class
(or at the end of the file). Do not modify any existing test — existing constructions of
`QueryResult(answer=..., sources=..., one_line_summary=...)` will continue to work because all new
fields have defaults.

### New test class

```python
class TestQueryResultCacheFields:
    """Tests for the three new cache-related fields on QueryResult."""

    def _make_minimal(self, **kwargs) -> QueryResult:
        """Helper: construct a QueryResult with minimum required positional fields."""
        return QueryResult(
            answer="The answer.",
            sources=["src/auth.py.md"],
            one_line_summary="Explains auth.",
            **kwargs,
        )

    def test_from_cache_defaults_to_false(self):
        result = self._make_minimal()
        assert result.from_cache is False

    def test_cached_path_defaults_to_none(self):
        result = self._make_minimal()
        assert result.cached_path is None

    def test_cached_at_defaults_to_none(self):
        result = self._make_minimal()
        assert result.cached_at is None

    def test_from_cache_can_be_set_true(self):
        result = self._make_minimal(from_cache=True)
        assert result.from_cache is True

    def test_cached_path_accepts_path_object(self):
        p = Path("queries/how-does-auth-work.md")
        result = self._make_minimal(cached_path=p)
        assert result.cached_path == p

    def test_cached_at_accepts_string(self):
        ts = "2026-04-29 10:00:00 UTC"
        result = self._make_minimal(cached_at=ts)
        assert result.cached_at == ts

    def test_existing_fields_unaffected(self):
        """Constructing QueryResult without cache fields works as before (AT-10)."""
        result = QueryResult(
            answer="Answer text.",
            sources=["src/x.py.md"],
            one_line_summary="One line.",
        )
        assert result.answer == "Answer text."
        assert result.sources == ["src/x.py.md"]
        assert result.one_line_summary == "One line."
        assert result.stale_warnings == []
        assert result.from_cache is False
        assert result.cached_path is None
        assert result.cached_at is None

    def test_full_cache_hit_construction(self):
        """All three cache fields set together — the shape check_query_cache() will use."""
        result = QueryResult(
            answer="Verbatim file content...",
            sources=["src/auth.py.md"],
            one_line_summary="Explains auth.",
            stale_warnings=[],
            from_cache=True,
            cached_path=Path("queries/how-does-auth-work.md"),
            cached_at="2026-04-29 10:00:00 UTC",
        )
        assert result.from_cache is True
        assert result.cached_path == Path("queries/how-does-auth-work.md")
        assert result.cached_at == "2026-04-29 10:00:00 UTC"
```

### Existing test assertions — no changes needed

All existing `TestRunQuery` tests construct `QueryResult` implicitly via `run_query()`, which
already passes only the four original fields. The new fields default to their "not a cache hit"
values, so no existing assertion breaks. Verify:

| Test | Assert on `result` | Impact of new fields |
|------|-------------------|----------------------|
| `test_successful_query_returns_query_result` | `isinstance(result, QueryResult)`, `result.answer`, `result.one_line_summary`, `result.sources` | No impact — new fields not checked |
| `test_select_relevant_files_returns_correct_subset` | `result.sources` membership | No impact |
| All `test_raises_*` tests | Exception type only | No impact |

No existing test explicitly checks `result.from_cache`, `result.cached_path`, or
`result.cached_at`, so none need updating.

---

## Notes

1. **Field ordering**: Python dataclasses require fields without defaults before fields with
   defaults. `answer`, `sources`, `one_line_summary` have no defaults (required positional args).
   `stale_warnings`, `from_cache`, `cached_path`, `cached_at` all have defaults. The ordering is
   correct and consistent with the existing pattern.

2. **`Path | None` vs `Optional[Path]`**: The codebase already uses `from __future__ import
   annotations`, so `Path | None` is safe without any additional imports. Do not use
   `Optional[Path]` — the project style uses the `X | Y` union form (see the `TYPE_CHECKING`
   imports block pattern in the file).

3. **No `field()` wrapper needed**: `False` and `None` are immutable, so they can be used as bare
   defaults without `field(default=False)` or `field(default=None)`. This matches the style of the
   existing module (compare: `stale_warnings: list[str] = field(default_factory=list)` uses
   `field()` only because `list` is mutable).

4. **`run_query()` return site**: The `run_query()` function (lines 401–406) constructs
   `QueryResult(answer=..., sources=..., one_line_summary=..., stale_warnings=...)`. This call site
   does NOT need to be changed — the three new cache fields default to their "not a cache hit"
   values automatically, which is exactly the correct behavior for a fresh (non-cached) query
   result.

5. **Downstream callers**: The plan intentionally does not modify `cli.py` or `mcp_server.py`.
   Those callers read `result.from_cache`, `result.cached_path`, and `result.cached_at` in later
   plans. The field additions here are purely additive and backward-compatible.
