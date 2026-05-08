# Implementation Catalog: Query Cache — Have I Answered This Before?

## Overview

This feature adds a two-stage cache pre-check to `run_query()` that detects existing saved query pages before making any LLM calls. Eight discrete items cover: promoting internal helpers to public API, extending the `QueryResult` dataclass, implementing the new `query_cache.py` module, extracting a shared log helper, integrating the cache into the query engine, and updating the CLI and MCP callers. A test update item rounds out the catalog.

---

## Catalog Items

### 1. Promote `has_stale_banner` to Public API

- **Purpose**: Renames `_has_stale_banner()` to `has_stale_banner()` in `staleness.py`, making it a public function that `query_cache.py` can import directly. Updates the existing test file to reference the new public name.
- **Scope**:
  - `codebase_wiki_builder/staleness.py`
  - `tests/test_staleness.py`
- **Blocks**: Item 3 (Query Cache Module)
- **Blocked by**: nothing
- **Acceptance Criteria**: AC-FR-QC-3 (staleness check callable from cache module); AT-4 and AT-11 indirectly depend on this being public.

---

### 2. Promote `parse_existing_index` to Public API

- **Purpose**: Renames `_parse_existing_index()` to `parse_existing_index()` in `index_writer.py`, making it a public function that `query_cache.py` can use in Stage 2 to read query page rows from `index.md`. Updates the existing test file to reference the new public name.
- **Scope**:
  - `codebase_wiki_builder/index_writer.py`
  - `tests/test_index_writer.py`
- **Blocks**: Item 3 (Query Cache Module)
- **Blocked by**: nothing
- **Acceptance Criteria**: AC-FR-QC-2 Stage 2 (index.md rows parsed by `parse_existing_index()`); AT-5, AT-7.

---

### 3. Extend `QueryResult` with Cache Fields

- **Purpose**: Adds three new fields to the `QueryResult` dataclass in `query_engine.py`: `from_cache: bool` (default `False`), `cached_path: Path | None` (default `None`), and `cached_at: str | None` (default `None`). No existing fields are removed or changed; all default values ensure backward compatibility.
- **Scope**:
  - `codebase_wiki_builder/query_engine.py`
  - `tests/test_query_engine.py` (verify `from_cache` defaults to `False`)
- **Blocks**: Items 4, 5, 6, 7 (everything that reads or produces a `QueryResult`)
- **Blocked by**: nothing
- **Acceptance Criteria**: AT-10 (from_cache defaults to False); FR-QC-4 schema change.

---

### 4. Extract `write_query_log_entry` Helper

- **Purpose**: Extracts the log-write logic from `save_query_page()` into a new public standalone helper `write_query_log_entry(question, vault_root, log_fn, cache_hit=False)` in `query_persistence.py`. Updates `save_query_page()` to call this helper instead of duplicating the logic. The log entry format for cache hits uses `cache-hit` as the entry type field.
- **Scope**:
  - `codebase_wiki_builder/query_persistence.py`
  - `tests/test_query_persistence.py`
- **Blocks**: Items 6 (MCP update), 7 (CLI update)
- **Blocked by**: nothing
- **Acceptance Criteria**: FR-QC-7a (standalone helper exists; both callers use it); AT-1c, AT-8 (log entry written on cache hit).

---

### 5. Implement `query_cache.py` — Core Cache Module

- **Purpose**: Creates the new `codebase_wiki_builder/query_cache.py` module containing the public function `check_query_cache(question, vault_root, index_content, llm_client, config) -> QueryResult | None`. Implements Stage 1 (slug normalization pass: slugify → scan slug/slug-2/slug-3 files → normalize question comparison → staleness check) and Stage 2 (LLM pre-check: parse query rows from `parse_existing_index()`, collect H1 titles via `read_query_page()`, call `llm_client.complete()` with conservative prompt, validate returned path with the 3-part SEC-3 check, staleness check). All errors are caught and result in a cache miss; LLM errors are logged at WARNING, parse errors at DEBUG.
- **Scope**:
  - `codebase_wiki_builder/query_cache.py` *(new file)*
  - `tests/test_query_cache.py` *(new file)*
- **Blocks**: Item 6 (integration into `run_query()`)
- **Blocked by**: Items 1 (`has_stale_banner` public), 2 (`parse_existing_index` public), 3 (`QueryResult` extended)
- **Acceptance Criteria**: AT-1 through AT-7, AT-11, AT-12, AT-15, AT-16, AT-17; FR-QC-1, FR-QC-2, FR-QC-3, FR-QC-8, FR-QC-9, SEC-3.

---

### 6. Integrate Cache Pre-Check into `run_query()`

- **Purpose**: Inserts a call to `check_query_cache()` inside `run_query()` in `query_engine.py`, placed after `_collect_stale_warnings()` is called (step 2) but before the first LLM call (step 3). If `check_query_cache()` returns a non-None `QueryResult`, `run_query()` returns it immediately, skipping all LLM calls. The `stale_warnings` already collected at step 2 are passed into `check_query_cache()` so they are included on cache-hit results.
- **Scope**:
  - `codebase_wiki_builder/query_engine.py`
  - `tests/test_query_engine.py`
- **Blocks**: Items 7 (CLI), 8 (MCP)
- **Blocked by**: Items 3 (`QueryResult` extended), 5 (`query_cache.py` implemented)
- **Acceptance Criteria**: FR-QC-5 (integration sequence); AT-1d (zero LLM calls on Stage 1 hit); AT-5 (one LLM call on Stage 2 hit); AT-6 (three LLM calls on full miss); AT-7 (no Stage 2 LLM call when no query pages exist).

---

### 7. Update CLI for Cache Hit Attribution

- **Purpose**: Modifies `_run_query_command()` in `cli.py` to detect `result.from_cache` on the returned `QueryResult`. On a cache hit: prints the `[cache] Answering from saved page: <path> (saved <timestamp>)` attribution line before the answer, suppresses the save prompt (`_prompt_save()`), and calls `write_query_log_entry()` with `cache_hit=True` to write the log entry. On a fresh result the existing behavior is unchanged.
- **Scope**:
  - `codebase_wiki_builder/cli.py`
  - `tests/test_cli.py`
- **Blocks**: nothing
- **Blocked by**: Items 4 (`write_query_log_entry` helper), 6 (`run_query()` integration)
- **Acceptance Criteria**: AT-1a, AT-1c, AT-13 (save prompt suppressed), AT-14 (stale warnings still shown); FR-QC-6.

---

### 8. Update MCP Handler for Cache Hit Response

- **Purpose**: Modifies `_handle_wiki_query()` in `mcp_server.py` to detect `result.from_cache` on the returned `QueryResult`. On a cache hit: skips the call to `save_query_page()`, calls `write_query_log_entry()` with `cache_hit=True`, and adds `cache_hit: true`, `cached_at: <timestamp>`, and `saved_path: <cached_path>` to the JSON response. On all responses (both cache hits and fresh results) ensures all 6 response fields are always present (`answer`, `saved_path`, `stale_warnings`, `cache_hit`, `cached_at`, `one_line_summary`).
- **Scope**:
  - `codebase_wiki_builder/mcp_server.py`
  - `tests/test_mcp_server.py`
- **Blocks**: nothing
- **Blocked by**: Items 4 (`write_query_log_entry` helper), 6 (`run_query()` integration)
- **Acceptance Criteria**: AT-8 (no duplicate file on cache hit), AT-9 (cache_hit false on fresh result); FR-QC-7 (MCP cache hit behavior and 6-field schema).

---

## Dependency Graph

```
Items 1, 2, 3, 4  (no blockers — all start immediately, in parallel)
         │
         ▼
     Item 5  (blocked by 1, 2, 3)
         │
         ▼
     Item 6  (blocked by 3, 5)
         │
    ┌────┴────┐
    ▼         ▼
 Item 7    Item 8
(blocked   (blocked
 by 4, 6)   by 4, 6)
```

Items 1, 2, 3, and 4 have no dependencies and can be built in parallel. Item 5 is the critical-path item (new module) and must be complete before Item 6 can integrate it. Items 7 and 8 are the leaf callers and can be built in parallel once Items 4 and 6 are done.

---

## Summary Table

| # | Name | Files Touched | Blocked By |
|---|------|---------------|------------|
| 1 | Promote `has_stale_banner` to Public API | `staleness.py`, `test_staleness.py` | — |
| 2 | Promote `parse_existing_index` to Public API | `index_writer.py`, `test_index_writer.py` | — |
| 3 | Extend `QueryResult` with Cache Fields | `query_engine.py`, `test_query_engine.py` | — |
| 4 | Extract `write_query_log_entry` Helper | `query_persistence.py`, `test_query_persistence.py` | — |
| 5 | Implement `query_cache.py` — Core Cache Module | `query_cache.py` (new), `test_query_cache.py` (new) | 1, 2, 3 |
| 6 | Integrate Cache Pre-Check into `run_query()` | `query_engine.py`, `test_query_engine.py` | 3, 5 |
| 7 | Update CLI for Cache Hit Attribution | `cli.py`, `test_cli.py` | 4, 6 |
| 8 | Update MCP Handler for Cache Hit Response | `mcp_server.py`, `test_mcp_server.py` | 4, 6 |
