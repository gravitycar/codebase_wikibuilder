# Implementation Plan: Integrate Cache Pre-Check into run_query()

## Spec Context

This plan implements FR-QC-5 from the query-cache specification. It inserts a single call to
`check_query_cache()` inside the existing `run_query()` function in `query_engine.py`, at the
precise point after `index.md` is read and `stale_warnings` are collected, but before the first
LLM call. This is the single integration point that gives both CLI and MCP callers cache hit
behavior transparently — neither caller needs to change to benefit from the cache.

Catalog item: Integrate Cache Pre-Check into run_query()
Specification section: FR-QC-5, FR-QC-8
Acceptance criteria addressed: AT-1 through AT-9, AT-14 (cache miss fallthrough is transparent;
stale_warnings are surfaced on cache hits)

---

## Dependencies

- **Blocked by**:
  - plan-03 (QueryResult cache fields) — `QueryResult` must already have `from_cache`,
    `cached_path`, `cached_at` fields before this plan is built.
  - plan-05 (query_cache module) — `check_query_cache()` must already exist in
    `codebase_wiki_builder/query_cache.py` before this plan is built.
- **Blocks**: plan-07 (CLI cache-hit behavior) and plan-08 (MCP cache-hit behavior) — both
  callers depend on `run_query()` returning `QueryResult.from_cache=True` on a hit.
- **Uses**:
  - `codebase_wiki_builder.query_cache.check_query_cache` — the new import added by this plan.
  - All existing imports in `query_engine.py` are unchanged.

---

## File Changes

### New Files

None.

### Modified Files

- `codebase_wiki_builder/query_engine.py` — Two changes:
  1. Add one import: `from codebase_wiki_builder.query_cache import check_query_cache`
  2. Insert the cache pre-check block (Step 2.5) inside `run_query()`.

---

## Implementation Details

### Import addition — `codebase_wiki_builder/query_engine.py`

Add `check_query_cache` to the module-level imports. Because `query_cache.py` imports
`QueryResult` from `query_engine.py` at runtime (inside function bodies to avoid circular
imports), the import here must be a normal top-level import — not under `TYPE_CHECKING`.

Place the new import after the existing `if TYPE_CHECKING:` block and before the
`logger = logging.getLogger(__name__)` line:

```python
# After the TYPE_CHECKING block (line ~31 in current query_engine.py)
from codebase_wiki_builder.query_cache import check_query_cache
```

**Why not under TYPE_CHECKING?** `run_query()` calls `check_query_cache()` at runtime, so the
import must be available at function call time. `TYPE_CHECKING` imports are not executed at
runtime.

**Circular import safety**: `query_cache.py` imports `QueryResult` from `query_engine.py` only
inside function bodies (deferred), not at module level. This breaks the circular dependency.
Python's import system can handle the top-level import here (`query_engine` → `query_cache`)
without a cycle because `query_cache` does not import `query_engine` at module level.

### Cache pre-check insertion — `run_query()` in `codebase_wiki_builder/query_engine.py`

The current `run_query()` function has this structure at the relevant point (lines ~354–370):

```python
    # Step 2: Read index.md, collect stale warnings (must run BEFORE LLM calls)
    index_content = index_path.read_text(encoding="utf-8")
    stale_warnings = _collect_stale_warnings(index_content)

    # Step 3: First LLM call — relevance identification
    # LLMError propagates to caller on fatal failure
    relevance_prompt = _build_relevance_prompt(question, index_content)
    raw_relevance = llm_client.complete(relevance_prompt)
    ...
```

Insert the new Step 2.5 block **between** the Step 2 comment block and the Step 3 comment block,
as follows:

```python
    # Step 2: Read index.md, collect stale warnings (must run BEFORE LLM calls)
    index_content = index_path.read_text(encoding="utf-8")
    stale_warnings = _collect_stale_warnings(index_content)

    # Step 2.5: Cache pre-check — return saved answer if available and not stale
    cache_result = check_query_cache(question, vault_root, index_content, llm_client, config)
    if cache_result is not None:
        cache_result.stale_warnings = stale_warnings
        return cache_result

    # Step 3: First LLM call — relevance identification
    # LLMError propagates to caller on fatal failure
    relevance_prompt = _build_relevance_prompt(question, index_content)
    raw_relevance = llm_client.complete(relevance_prompt)
    ...
```

**Why assign `stale_warnings` after the call?** `check_query_cache()` receives `index_content`
and internally re-derives `stale_warnings` from it (using its own private helper). However, the
spec (FR-QC-4) is explicit: "stale_warnings SHALL be the existing stale warnings collected by
`_collect_stale_warnings(index_content)` (already computed at the top of `run_query()`)". By
overwriting `cache_result.stale_warnings` here, we guarantee the authoritative copy computed by
`run_query()` is the one that reaches the caller — regardless of any differences in the internal
helper. This is a belt-and-suspenders correctness guarantee with zero overhead.

**Nothing else changes.** Steps 1, 2, 3, 4, 5, 6, 7, and 8 are completely unmodified. The fresh
query path (cache miss) falls through to Step 3 exactly as before. The `QueryResult` constructed
at the end of `run_query()` (line ~401) already leaves `from_cache=False`, `cached_path=None`,
and `cached_at=None` by default — no change needed there.

### Updated docstring for `run_query()`

Update the `Steps:` list in the existing docstring to reflect the new step:

```python
    """Run the full two-LLM-call query workflow.

    Steps:
      1. Check index.md exists (raises FileNotFoundError if not).
      2. Read index.md; collect stale_warnings.
      2.5. Cache pre-check: call check_query_cache(). If a non-stale cached answer is
           found, copy stale_warnings into the result and return immediately.
      3. First LLM call: identify relevant files as JSON array sorted by relevance descending.
      4. Raise NoRelevantFilesError if LLM returns empty array.
      5. Fill context budget using tiktoken (QUERY_CONTEXT_WINDOW = 128_000 tokens).
         - Skip files that exceed the budget by themselves → annotate as (too large to include).
         - Stop filling when budget would be exceeded → track overflow count.
      6. Second LLM call: answer question + one-line summary.
      7. Build ## Sources section and overflow note.
      8. Return QueryResult.

    Raises:
        FileNotFoundError: if index.md does not exist in vault_root.
        NoRelevantFilesError: if the LLM identifies no relevant files.
        LLMError: on fatal LLM API failures.
    """
```

---

## Error Handling

No new error handling is needed in `run_query()`. All error handling for the cache lookup is
encapsulated within `check_query_cache()` (per FR-QC-9 and plan-05). The function is contractually
guaranteed to never raise — any internal exception produces a `None` return, which causes
`run_query()` to fall through to the full pipeline as if the cache did not exist.

| Condition | Behavior |
|-----------|----------|
| `check_query_cache()` returns `None` (any reason) | Fall through to Step 3 — full pipeline runs |
| `check_query_cache()` returns a `QueryResult` | Copy `stale_warnings`, return immediately |
| `check_query_cache()` raises (should never happen) | Not possible by contract; would propagate as unexpected error — acceptable |

---

## Unit Test Specifications

**File**: `tests/test_query_engine.py`

Add a new test class `TestRunQueryCacheIntegration` to the existing test file. The tests use
`unittest.mock.patch` to control `check_query_cache()` behavior without needing a real vault.

All existing tests in `TestRunQuery` are unaffected — they do not patch `check_query_cache()`,
so the real implementation runs; it returns `None` because `tmp_path` vaults have no
`queries/` files, causing the full pipeline to run as before.

### New test class

```python
class TestRunQueryCacheIntegration:
    """Tests for the check_query_cache() integration point in run_query()."""
```

### Test cases

| Test | Setup | Expected | Why |
|------|-------|----------|-----|
| `test_cache_hit_returns_cached_result` | `check_query_cache` returns a `QueryResult` with `from_cache=True` | `run_query()` returns that result; `llm_client.complete` is never called | AT-1: cache hit skips both LLM calls |
| `test_cache_hit_overwrites_stale_warnings` | Cache mock returns `QueryResult(stale_warnings=["old"])`, `_collect_stale_warnings` returns `["fresh"]` | Returned result has `stale_warnings == ["fresh"]` | AT-14: stale_warnings from `run_query()` take precedence |
| `test_cache_miss_runs_full_pipeline` | `check_query_cache` returns `None` | `llm_client.complete` called twice; `QueryResult.from_cache` is `False` | AT-6/FR-QC-8: miss is transparent |
| `test_cache_hit_stale_warnings_empty_list` | Cache mock returns result; `_collect_stale_warnings` returns `[]` | `stale_warnings == []` on returned result | No spurious warnings on hit with no stale pages |
| `test_cache_receives_correct_args` | Spy on `check_query_cache` | Called with `(question, vault_root, index_content, llm_client, config)` | Verify correct arguments are forwarded |

### Key scenario: cache hit suppresses LLM calls (AT-1)

```python
def test_cache_hit_returns_cached_result(self, tmp_path, monkeypatch):
    """When check_query_cache returns a QueryResult, run_query returns it immediately
    without calling llm_client.complete()."""
    from unittest.mock import MagicMock, patch
    from codebase_wiki_builder.query_engine import run_query, QueryResult

    # Write a minimal index.md so Step 1 and Step 2 succeed
    (tmp_path / "index.md").write_text(
        "| File | Description |\n|------|-------------|\n",
        encoding="utf-8",
    )

    cached = QueryResult(
        answer="Cached answer content.",
        sources=["src/auth.py.md"],
        one_line_summary="Explains auth.",
        stale_warnings=[],
        from_cache=True,
        cached_path=Path("queries/how-does-auth-work.md"),
        cached_at="2026-04-29 10:00:00 UTC",
    )

    mock_llm = MagicMock()

    with patch(
        "codebase_wiki_builder.query_engine.check_query_cache",
        return_value=cached,
    ):
        result = run_query(
            question="How does auth work?",
            vault_root=tmp_path,
            llm_client=mock_llm,
            config=MagicMock(),
        )

    assert result is cached
    assert result.from_cache is True
    mock_llm.complete.assert_not_called()
```

### Key scenario: stale_warnings overwrite (AT-14)

```python
def test_cache_hit_overwrites_stale_warnings(self, tmp_path, monkeypatch):
    """run_query() replaces the stale_warnings on the cache result with the
    authoritative list computed by _collect_stale_warnings(index_content)."""
    from unittest.mock import MagicMock, patch
    from codebase_wiki_builder.query_engine import run_query, QueryResult

    # index.md with a stale entry so _collect_stale_warnings returns something real
    (tmp_path / "index.md").write_text(
        "| File | Description |\n|------|-------------|\n"
        "| [[queries/old-query]] | Old answer ⚠ stale |\n",
        encoding="utf-8",
    )

    cached = QueryResult(
        answer="Cached answer.",
        sources=[],
        one_line_summary="Explains something.",
        stale_warnings=["queries/this-should-be-replaced.md"],  # stale from cache module
        from_cache=True,
        cached_path=Path("queries/some-query.md"),
        cached_at="2026-04-29 10:00:00 UTC",
    )

    with patch(
        "codebase_wiki_builder.query_engine.check_query_cache",
        return_value=cached,
    ):
        result = run_query(
            question="Some question?",
            vault_root=tmp_path,
            llm_client=MagicMock(),
            config=MagicMock(),
        )

    # stale_warnings should be the authoritative list from run_query()'s own scan
    assert result.stale_warnings == ["queries/old-query.md"]
```

### Key scenario: cache miss falls through (FR-QC-8)

```python
def test_cache_miss_runs_full_pipeline(self, tmp_path):
    """When check_query_cache returns None, the full two-LLM-call pipeline runs."""
    from unittest.mock import MagicMock, patch
    from codebase_wiki_builder.query_engine import run_query

    (tmp_path / "index.md").write_text(
        "| File | Description |\n|------|-------------|\n"
        "| [[src/auth.py]] | Auth module |\n",
        encoding="utf-8",
    )
    # Create a dummy summary file that the LLM will "select"
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py.md").write_text("Auth content.", encoding="utf-8")

    mock_llm = MagicMock()
    mock_llm.complete.side_effect = [
        '["src/auth.py.md"]',  # relevance call
        '{"answer": "Fresh answer.", "one_line_summary": "Explains auth."}',  # answer call
    ]

    with patch(
        "codebase_wiki_builder.query_engine.check_query_cache",
        return_value=None,
    ):
        result = run_query(
            question="How does auth work?",
            vault_root=tmp_path,
            llm_client=mock_llm,
            config=MagicMock(),
        )

    assert result.from_cache is False
    assert mock_llm.complete.call_count == 2
```

---

## Notes

1. **Minimal diff**: This plan makes the smallest possible change to `query_engine.py`:
   one import line and four lines of code inside `run_query()`. The fresh-query path is
   completely unmodified. All existing tests pass without changes.

2. **`stale_warnings` assignment is correct even on empty list**: The line
   `cache_result.stale_warnings = stale_warnings` runs whether `stale_warnings` is empty or not.
   This is correct — an empty list is still the authoritative answer from `run_query()`.

3. **Import placement**: The `check_query_cache` import goes after the `if TYPE_CHECKING:` block
   and the `logger` assignment in the file's "module-level" section. Exact placement: after the
   closing `endif` of the TYPE_CHECKING block (around line 31 in current file), before the
   module-level constants section (`QUERY_CONTEXT_WINDOW = 128_000`).

4. **`dataclass` mutation is safe**: `QueryResult` is a plain `@dataclass` (not frozen), so
   `cache_result.stale_warnings = stale_warnings` is valid Python. No `replace()` or
   `copy()` is needed.

5. **Test isolation**: The new test class patches `check_query_cache` at the
   `codebase_wiki_builder.query_engine` namespace (not at `codebase_wiki_builder.query_cache`).
   This is the correct patch target because `run_query()` calls the name as imported into the
   `query_engine` module's namespace.

6. **No change to the `run_query()` return at Step 8**: The `QueryResult(...)` construction at
   the end of `run_query()` already omits `from_cache`, `cached_path`, and `cached_at`, so they
   default to `False`, `None`, and `None` respectively (per plan-03). No modification needed.

7. **`config` parameter forwarding**: `run_query()` already accepts `config: "WikiConfig"` as a
   parameter. It is forwarded directly to `check_query_cache()` as the fifth argument. No
   additional handling is required.
