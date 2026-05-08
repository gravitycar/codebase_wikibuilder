# Implementation Plan: Update MCP Handler for Cache Hit Response

## Spec Context

This plan implements FR-QC-7 and the MCP response schema change from the query-cache
specification (v1.4.0). It modifies `_handle_wiki_query()` in `mcp_server.py` to:

1. Skip `save_query_page()` when `result.from_cache is True`.
2. Call `write_query_log_entry()` with `cache_hit=True` on a cache hit.
3. Always return all 7 fields in the JSON response (`answer`, `saved_path`, `stale_warnings`,
   `cache_hit`, `cached_at`, `one_line_summary`, `sources`) — both on fresh results and cache hits.

Catalog item: Update MCP Handler for Cache Hit Response
Specification section: FR-QC-7, FR-QC-7a; also the MCP wiki_query Response Schema Change table
Acceptance criteria addressed: AT-8 (MCP cache hit — no duplicate file), AT-9 (MCP fresh result
— cache_hit false), SC-4 (MCP does not create duplicate files), SC-6 (MCP returns all 7 fields
always)

---

## Dependencies

- **Blocked by**:
  - plan-03 (QueryResult cache fields) — `result.from_cache`, `result.cached_path`,
    `result.cached_at`, and `result.one_line_summary` must exist on `QueryResult` before this
    plan is built.
  - plan-04 (write_query_log_entry helper) — `write_query_log_entry()` must be exported from
    `query_persistence.py` before the MCP handler can call it.
  - plan-06 (run_query integration) — `run_query()` must return `QueryResult.from_cache=True` on
    a cache hit before this plan is useful.
- **Blocks**: Nothing. This is the final plan in the MCP chain.
- **Uses**:
  - `codebase_wiki_builder.query_persistence.write_query_log_entry` — new import.
  - `codebase_wiki_builder.query_persistence.save_query_page` — existing import, retained for
    the fresh-query path.
  - `result.from_cache`, `result.cached_path`, `result.cached_at`, `result.one_line_summary`,
    `result.stale_warnings` — fields on `QueryResult` (added by plan-03).

---

## File Changes

### New Files

None.

### Modified Files

- `codebase_wiki_builder/mcp_server.py` — Two changes:
  1. Add `write_query_log_entry` to the import from `query_persistence`.
  2. Replace Step 4 and Step 5 of `_handle_wiki_query()` with the new branched logic.

---

## Implementation Details

### Import update — `codebase_wiki_builder/mcp_server.py`

**Current import** (line 34):

```python
from codebase_wiki_builder.query_persistence import save_query_page
```

**Updated import**:

```python
from codebase_wiki_builder.query_persistence import save_query_page, write_query_log_entry
```

`write_query_log_entry` is added to the same import line. No other import changes are needed.

---

### Revised `_handle_wiki_query()` — Step 4 and Step 5

The current function has two steps after `run_query()` succeeds:

- **Step 4** (lines 188–199): unconditionally calls `save_query_page()` and raises `McpError`
  on failure.
- **Step 5** (lines 201–211): builds and returns the JSON response with 4 fields (`answer`,
  `sources`, `saved_path`, `stale_warning`).

Both steps are replaced in full. Steps 1–3 (parameter validation, question extraction,
`run_query()` call) are **unchanged**.

**Replacement logic** (replace everything from the `# Step 4` comment through the final
`return` statement):

```python
    # Step 4 — Branch on cache hit vs fresh query
    if result.from_cache:
        # Cache hit: skip save, just log and return the cached result
        saved_path_str = str(result.cached_path) if result.cached_path is not None else ""
        try:
            write_query_log_entry(
                question=question,
                vault_root=vault_root,
                log_fn=log_fn,
                cache_hit=True,
                cached_path=saved_path_str,
            )
        except Exception as exc:
            logger.exception("Failed to write cache-hit log entry: %s", exc)
            raise mcp.shared.exceptions.McpError(
                mcp.types.ErrorData(
                    code=mcp.types.INTERNAL_ERROR,
                    message=f"Cache hit but failed to write log: {exc}",
                )
            ) from exc
        cache_hit_flag = True
        cached_at_value = result.cached_at  # str | None
    else:
        # Fresh query: save automatically via save_query_page()
        try:
            saved_page = save_query_page(question, result, vault_root, log_fn)
            saved_path_str = saved_page.relative_to(vault_root).as_posix()
        except Exception as exc:
            logger.exception("Failed to save query page: %s", exc)
            raise mcp.shared.exceptions.McpError(
                mcp.types.ErrorData(
                    code=mcp.types.INTERNAL_ERROR,
                    message=f"Answer generated but failed to save: {exc}",
                )
            ) from exc
        cache_hit_flag = False
        cached_at_value = None

    # Step 5 — Build and return JSON response (always 7 fields)
    response_obj = {
        "answer": result.answer,
        "saved_path": saved_path_str,
        "stale_warnings": result.stale_warnings if result.stale_warnings else [],
        "cache_hit": cache_hit_flag,
        "cached_at": cached_at_value,
        "one_line_summary": result.one_line_summary,
        "sources": result.sources if result.sources else [],
    }

    return [mcp.types.TextContent(type="text", text=json.dumps(response_obj, ensure_ascii=False))]
```

**Key design decisions**:

1. **`saved_path_str` on cache hit**: `result.cached_path` is a `Path` object (vault-relative
   path of the matched page, e.g. `queries/how-does-auth-work.md`). Convert with `str()` to get
   the POSIX string. If `cached_path` is `None` (should not happen on a real cache hit, but
   defensively handled), fall back to `""`.

2. **`stale_warnings` field**: Always a list. The old code used `None` as sentinel; the new
   schema uses an empty list `[]` when there are no warnings. This is a schema improvement
   (stable type).

3. **`cached_at_value`**: On cache hit, this is `result.cached_at` — a `str | None` as populated
   by `check_query_cache()`. It is passed through as-is; `json.dumps` serializes `None` as
   `null`, which is the correct JSON representation.

4. **`one_line_summary`**: Always included. On fresh queries, `result.one_line_summary` is the
   LLM-generated one-line summary. On cache hits, it is the description from the matching
   `index.md` row (populated by `check_query_cache()`).

5. **Log error handling on cache hit**: If `write_query_log_entry()` raises, the MCP handler
   raises `McpError(INTERNAL_ERROR)` — consistent with the existing behavior when `save_query_page()`
   fails. The spec's constraint "Do NOT skip the log.md entry for cache hits" is enforced.

6. **`sources` field retained**: The old response included a `sources` field and a `stale_warning`
   (singular, nullable) field. The new schema replaces `stale_warning` with `stale_warnings`
   (always a list) and keeps `sources` as the 7th field. `sources` is populated from
   `result.sources` on both cache hits and fresh queries; an empty list `[]` is used when
   `result.sources` is falsy. This is consistent with the 7-field schema in FR-QC-7.

---

### Updated module docstring reference

No docstring changes are strictly needed. The function docstring for `_handle_wiki_query()` can
be updated to reflect the new behavior:

```python
    """MCP tool handler for wiki_query.

    On a cache hit (result.from_cache is True):
      - Skips save_query_page().
      - Calls write_query_log_entry() with cache_hit=True.
      - Returns all 7 fields with cache_hit=True and the cached_at timestamp.

    On a fresh query (result.from_cache is False):
      - Saves automatically via save_query_page().
      - Returns all 7 fields with cache_hit=False and cached_at=null.

    Always returns a list containing a single TextContent with a JSON-encoded response
    object containing all 7 fields: answer, saved_path, stale_warnings, cache_hit,
    cached_at, one_line_summary, sources.
    Raises McpError for all error conditions (invalid params, query failures, save/log failures).
    """
```

---

## Error Handling

| Condition | Behavior |
|-----------|----------|
| `result.from_cache is True` and `write_query_log_entry()` raises | `McpError(INTERNAL_ERROR)` with message "Cache hit but failed to write log: {exc}" |
| `result.from_cache is False` and `save_query_page()` raises | `McpError(INTERNAL_ERROR)` with message "Answer generated but failed to save: {exc}" (same as before) |
| `result.cached_path is None` on cache hit | `saved_path_str = ""`; no error raised |
| `result.cached_at is None` on cache hit | `cached_at_value = None`; JSON renders as `null`; no error raised |
| `result.stale_warnings` is empty list | `stale_warnings: []` in response; no error |

---

## Unit Test Specifications

**File**: `tests/test_mcp_server.py` (extend existing file)

### New test class

Add `TestHandleWikiQueryCacheHit` to the existing test file, alongside any existing
`TestHandleWikiQuery` class.

### Test cases

| Test | Setup | Expected | Why |
|------|-------|----------|-----|
| `test_cache_hit_skips_save_query_page` | `run_query` returns `QueryResult(from_cache=True, cached_path=Path("queries/auth.md"), cached_at="2026-04-29 10:00:00 UTC", ...)` | `save_query_page` is NOT called | AT-8, SC-4 |
| `test_cache_hit_calls_write_query_log_entry` | Same cache-hit setup | `write_query_log_entry` called with `cache_hit=True, cached_path="queries/auth.md"` | FR-QC-7 step 6 |
| `test_cache_hit_response_fields` | Same cache-hit setup | Response JSON has all 7 fields; `cache_hit=true`; `cached_at` is non-null timestamp | AT-8, SC-6 |
| `test_cache_hit_saved_path_from_cached_path` | `cached_path=Path("queries/how-does-auth-work.md")` | `saved_path == "queries/how-does-auth-work.md"` | FR-QC-7 step 4 |
| `test_fresh_result_cache_hit_false` | `run_query` returns `QueryResult(from_cache=False, ...)` | `cache_hit=false`, `cached_at=null` in JSON | AT-9 |
| `test_fresh_result_calls_save_query_page` | Same fresh setup | `save_query_page` IS called | Existing behavior preserved |
| `test_fresh_result_all_6_fields_present` | Any fresh query | All 7 keys present in response JSON | SC-6 |
| `test_cache_hit_cached_at_null_when_none` | `cached_at=None` on cache hit | `cached_at: null` in JSON; still `cache_hit: true` | OQ-3 |
| `test_stale_warnings_always_list` | Empty `stale_warnings` | `stale_warnings: []` (not `null`) | Stable schema |

### Key scenario: cache hit skips save (AT-8)

```python
def test_cache_hit_skips_save_query_page(self, tmp_path):
    from unittest.mock import MagicMock, patch, AsyncMock
    from pathlib import Path
    from codebase_wiki_builder.mcp_server import _handle_wiki_query
    from codebase_wiki_builder.query_engine import QueryResult

    cached_result = QueryResult(
        answer="# How does auth work?\n\nAuth uses JWT.\n\n## Sources\n- src/auth.py.md\n",
        sources=["src/auth.py.md"],
        one_line_summary="Explains JWT auth.",
        stale_warnings=[],
        from_cache=True,
        cached_path=Path("queries/how-does-auth-work.md"),
        cached_at="2026-04-29 10:00:00 UTC",
    )

    mock_llm = MagicMock()
    mock_config = MagicMock()
    mock_log = MagicMock()

    with patch("codebase_wiki_builder.mcp_server.run_query", return_value=cached_result), \
         patch("codebase_wiki_builder.mcp_server.save_query_page") as mock_save, \
         patch("codebase_wiki_builder.mcp_server.write_query_log_entry") as mock_log_entry:
        import asyncio
        result = asyncio.run(_handle_wiki_query(
            {"question": "How does auth work?"},
            vault_root=tmp_path,
            llm_client=mock_llm,
            config=mock_config,
            log_fn=mock_log,
        ))

    mock_save.assert_not_called()
    mock_log_entry.assert_called_once_with(
        question="How does auth work?",
        vault_root=tmp_path,
        log_fn=mock_log,
        cache_hit=True,
        cached_path="queries/how-does-auth-work.md",
    )
```

### Key scenario: fresh result has all 7 fields (AT-9, SC-6)

```python
def test_fresh_result_all_6_fields_present(self, tmp_path):
    import json
    from unittest.mock import MagicMock, patch
    from pathlib import Path
    from codebase_wiki_builder.mcp_server import _handle_wiki_query
    from codebase_wiki_builder.query_engine import QueryResult

    fresh_result = QueryResult(
        answer="Fresh answer.\n\n## Sources\n- src/auth.py.md\n",
        sources=["src/auth.py.md"],
        one_line_summary="Explains auth freshly.",
        stale_warnings=[],
        from_cache=False,
    )
    saved_page = tmp_path / "queries" / "how-does-auth-work.md"
    saved_page.parent.mkdir(parents=True, exist_ok=True)
    saved_page.touch()

    with patch("codebase_wiki_builder.mcp_server.run_query", return_value=fresh_result), \
         patch("codebase_wiki_builder.mcp_server.save_query_page", return_value=saved_page), \
         patch("codebase_wiki_builder.mcp_server.write_query_log_entry"):
        import asyncio
        result = asyncio.run(_handle_wiki_query(
            {"question": "How does auth work?"},
            vault_root=tmp_path,
            llm_client=MagicMock(),
            config=MagicMock(),
            log_fn=MagicMock(),
        ))

    response = json.loads(result[0].text)
    assert set(response.keys()) == {"answer", "saved_path", "stale_warnings", "cache_hit", "cached_at", "one_line_summary", "sources"}
    assert response["cache_hit"] is False
    assert response["cached_at"] is None
    assert isinstance(response["stale_warnings"], list)
```

### Key scenario: cache_hit response fields (AT-8)

```python
def test_cache_hit_response_fields(self, tmp_path):
    import json
    from unittest.mock import MagicMock, patch
    from pathlib import Path
    from codebase_wiki_builder.mcp_server import _handle_wiki_query
    from codebase_wiki_builder.query_engine import QueryResult

    cached_result = QueryResult(
        answer="Cached answer body.",
        sources=[],
        one_line_summary="Cached summary.",
        stale_warnings=["queries/old.md"],
        from_cache=True,
        cached_path=Path("queries/how-does-auth-work.md"),
        cached_at="2026-04-29 10:00:00 UTC",
    )

    with patch("codebase_wiki_builder.mcp_server.run_query", return_value=cached_result), \
         patch("codebase_wiki_builder.mcp_server.save_query_page") as mock_save, \
         patch("codebase_wiki_builder.mcp_server.write_query_log_entry"):
        import asyncio
        result = asyncio.run(_handle_wiki_query(
            {"question": "How does auth work?"},
            vault_root=tmp_path,
            llm_client=MagicMock(),
            config=MagicMock(),
            log_fn=MagicMock(),
        ))

    mock_save.assert_not_called()
    response = json.loads(result[0].text)
    assert response["cache_hit"] is True
    assert response["cached_at"] == "2026-04-29 10:00:00 UTC"
    assert response["saved_path"] == "queries/how-does-auth-work.md"
    assert response["one_line_summary"] == "Cached summary."
    assert response["stale_warnings"] == ["queries/old.md"]
```

---

## Notes

1. **`stale_warning` → `stale_warnings` rename**: The old response used `stale_warning` (singular,
   nullable). The new schema uses `stale_warnings` (plural, always a list). This is a breaking
   change for any existing MCP caller that reads `stale_warning`. The spec mandates the new
   schema; callers must be updated.

2. **`sources` field kept in response**: The old code included `"sources": result.sources`
   in the response. The new 7-field schema retains `sources` as the 7th field. `sources` is
   always a list (empty list when falsy). No changes are needed for callers that already read
   `sources` from the MCP response.

3. **`str(result.cached_path)` on a `Path` object**: `Path("queries/how-does-auth-work.md")`
   stringifies to `"queries/how-does-auth-work.md"` on all platforms (forward slash preserved).
   Using `str()` rather than `.as_posix()` is safe here because `cached_path` is always a
   vault-relative path with forward slashes (stored as-is from `query_cache.py`).

4. **`write_query_log_entry` is not async**: It is a synchronous function. Calling it directly
   inside an `async` handler is fine — it is a fast I/O call (`log_fn` writes a single line) and
   does not block the event loop meaningfully. Consistent with how `save_query_page()` is called
   synchronously inside the async handler today.

5. **Error handling symmetry**: Both the cache-hit log-write failure and the fresh-query save
   failure raise `McpError(INTERNAL_ERROR)`. This gives callers a consistent failure mode.

6. **`write_query_log_entry` import placement**: Add to the same `from query_persistence import`
   line as `save_query_page`. This avoids a second import statement and matches the project's
   single-import-per-module style.

7. **Test patching**: Patch `write_query_log_entry` at the `codebase_wiki_builder.mcp_server`
   namespace (i.e., `patch("codebase_wiki_builder.mcp_server.write_query_log_entry")`), not at
   `codebase_wiki_builder.query_persistence`. This is the correct target because `mcp_server.py`
   imports the name directly into its own namespace.
