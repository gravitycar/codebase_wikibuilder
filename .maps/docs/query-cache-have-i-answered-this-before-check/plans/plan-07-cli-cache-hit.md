# Implementation Plan: Update CLI for Cache Hit Attribution

## Spec Context

When `run_query()` returns a `QueryResult` with `from_cache=True`, the CLI must surface cache attribution to the user, suppress the save prompt (since the page already exists), and write a log entry via `write_query_log_entry()` rather than the current inline log-write code.

Catalog item: Update CLI for Cache Hit Attribution  
Specification section: FR-QC-6, FR-QC-7a  
Acceptance criteria addressed: AT-1(a)(c), AT-13, AT-14

---

## Dependencies

- **Blocked by**: plan-04 (`write_query_log_entry()` must exist in `query_persistence.py`)
- **Blocked by**: plan-06 (`run_query()` must return `from_cache=True` on cache hits; `result.cached_path` and `result.cached_at` must be populated)
- **Uses**: existing `_run_query_command()` in `cli.py`; existing `_prompt_save()`; existing `save_query_page()`

---

## File Changes

### New Files

None.

### Modified Files

- `codebase_wiki_builder/cli.py` — Update `_run_query_command()` to branch on `result.from_cache`

---

## Implementation Details

### `_run_query_command()` in `cli.py`

**File**: `codebase_wiki_builder/cli.py`

The current function (lines 464–534) follows this sequence after a successful `run_query()` call:

1. Print stale-page warnings (if any)
2. Print the answer
3. Prompt user to save (`_prompt_save()`)
4. Optionally call `save_query_page()` and print save confirmation
5. Append a per-query log entry inline via `log_fn()`
6. Exit 0

The change adds a `from_cache` branch immediately after the `run_query()` call (after step 5 in the current code, i.e. after stale warnings) and before step 7 (prompt). The two paths diverge after printing the answer.

**Updated import block** — add `write_query_log_entry` to the `query_persistence` import:

```python
from codebase_wiki_builder.query_persistence import save_query_page, write_query_log_entry
```

**Updated `_run_query_command()` body** — replace steps 5–9 (the save prompt, save action, and inline log entry) with a branching block:

```python
    # 5. Print stale-page warnings BEFORE the answer (per FR-5) — unchanged
    if result.stale_warnings:
        stale_list = ", ".join(result.stale_warnings)
        count = len(result.stale_warnings)
        console.print(
            f"[yellow]⚠ {count} query page(s) are stale: {stale_list} — "
            "run codewiki lint to update.[/yellow]"
        )

    # 6. Cache hit path
    if result.from_cache:
        # 6a. Print cache attribution before the answer
        cached_at_str = result.cached_at or "unknown"
        console.print(
            f"[cache] Answering from saved page: {result.cached_path} (saved {cached_at_str})"
        )

        # 6b. Print the answer (same as fresh query)
        console.print(result.answer)

        # 6c. Skip save prompt entirely — page already exists

        # 6d. Write log entry with cache_hit=True
        write_query_log_entry(
            question,
            vault_root,
            log_fn,
            cache_hit=True,
            cached_path=str(result.cached_path),
        )

        raise typer.Exit(code=0)

    # 7. Fresh query path (no changes to existing behaviour)
    # Print the answer
    console.print(result.answer)

    # 8. Prompt user to save (default No)
    save = _prompt_save()

    # 9. Optionally save the query page
    if save:
        saved_path = save_query_page(question, result, vault_root, log_fn)
        rel = saved_path.relative_to(vault_root).as_posix()
        console.print(f"[green]Answer saved to {rel}[/green]")

    # 10. Append per-query log.md entry (FR-6.1)
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    sources_summary = ", ".join(result.sources[:5])
    if len(result.sources) > 5:
        sources_summary += f" (and {len(result.sources) - 5} more)"
    log_fn(f"{ts} | query | {question} | sources: {sources_summary}")

    # 11. Exit 0 — normal completion
    raise typer.Exit(code=0)
```

**Key decisions**:

- The attribution line uses plain `console.print()` with no Rich markup around the `[cache]` prefix (it is literal text, not a Rich style tag). To avoid Rich interpreting `[cache]` as a markup tag, escape it or use `markup=False`. Use `console.print(f"[cache] Answering from ...", markup=False)` — or equivalently, escape with `\[cache]` in the f-string. Using `markup=False` is cleaner and explicit.
- Stale warnings are printed before the cache attribution line, preserving FR-QC-6 clause 3 ("print stale-page warnings as normal").
- The `write_query_log_entry()` call for cache hits passes `cached_path=str(result.cached_path)` (converting `Path` to `str`). Per plan-04, the signature is `write_query_log_entry(question, vault_root, log_fn, cache_hit=False, cached_path=None)`.
- The fresh-query path is entirely unchanged. The existing `log_fn(f"{ts} | query | ...")` inline log write remains as-is for fresh queries (plan-04 says `write_query_log_entry` is called by `save_query_page()` internally for fresh+saved queries, but the CLI's unconditional inline log for fresh+unsaved queries stays put). Verify this against plan-04 before building: if plan-04 moves the inline fresh-query log write into `save_query_page()` or a separate helper, the fresh path may also need updating. For this plan, treat the fresh path as unchanged.
- `datetime` and `timezone` imports already present in `_run_query_command()` — no new imports needed beyond `write_query_log_entry`.

**Attribution line format** (per FR-QC-6 and spec scenario):

```
[cache] Answering from saved page: queries/how-does-auth-work.md (saved 2026-04-29 10:00:00 UTC)
```

The spec FR-QC-6.1 states: `[cache] Answering from saved page: <vault-relative path> (saved <saved_at timestamp>)`. Follow the spec format exactly.

---

## Error Handling

- `result.cached_path` is guaranteed non-`None` when `result.from_cache is True` (populated by `check_query_cache()`). No null-guard needed, but `str(result.cached_path)` is safe even for `Path` objects.
- `result.cached_at` may be `None` (missing `saved_at` in the cached page per OQ-3). The `result.cached_at or "unknown"` expression handles this correctly.
- `write_query_log_entry()` handles its own errors internally (per plan-04). No exception handling needed at the call site.

---

## Unit Test Specifications

### `_run_query_command()` — cache hit path

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Cache hit with `cached_at` | `result.from_cache=True`, `cached_path=Path("queries/foo.md")`, `cached_at="2026-01-01 10:00:00 UTC"` | Attribution line printed with path and timestamp; answer printed; no save prompt; `write_query_log_entry` called with `cache_hit=True` | Happy path cache hit |
| Cache hit with `cached_at=None` | `result.from_cache=True`, `cached_path=Path("queries/foo.md")`, `cached_at=None` | Attribution line shows "unknown" for timestamp | Missing `saved_at` fallback |
| Cache hit — save prompt suppressed | `result.from_cache=True` | `_prompt_save()` is NOT called | AC AT-13 |
| Cache hit — stale warnings still shown | `result.from_cache=True`, `result.stale_warnings=["queries/old.md"]` | Stale warning line is printed before attribution | AC AT-14 |
| Cache hit — `save_query_page` not called | `result.from_cache=True` | `save_query_page()` is NOT called | Prevents duplicate file creation |
| Cache hit — exit code 0 | `result.from_cache=True` | `typer.Exit(code=0)` raised | Normal completion |
| Fresh query — unchanged | `result.from_cache=False` | Existing behaviour: answer printed, save prompt shown, log entry written inline | No regression |

### Key Scenario: Cache Hit with Missing `cached_at`

**Setup**: Mock `run_query()` to return `QueryResult(from_cache=True, cached_path=Path("queries/how-does-auth-work.md"), cached_at=None, answer="...", sources=[], one_line_summary="", stale_warnings=[])`  
**Action**: Call `_run_query_command("How does auth work?", vault_root)`  
**Expected**:  
- Console output contains `"[cache] Answering from saved page: queries/how-does-auth-work.md (saved unknown)"`  
- `write_query_log_entry` called with `cache_hit=True`, `cached_path="queries/how-does-auth-work.md"`  
- `_prompt_save()` not called  
- Exit code 0

### Key Scenario: Cache Hit — Stale Warnings Still Surfaced

**Setup**: `result.from_cache=True`, `result.stale_warnings=["queries/old-page.md"]`, `result.cached_path=Path("queries/current.md")`, `result.cached_at="2026-01-01 10:00:00 UTC"`  
**Action**: Call `_run_query_command(...)`  
**Expected**: Stale warning line (`⚠ 1 query page(s) are stale: queries/old-page.md`) appears BEFORE the `[cache]` attribution line in the output.

---

## Notes

- Rich markup escaping: `[cache]` must not be interpreted as a Rich style tag. Use `console.print(..., markup=False)` for the attribution line. Alternatively, escape as `\[cache]` in the format string. `markup=False` is recommended for clarity.
- The fresh-query inline log write (`log_fn(f"{ts} | query | ...")`) is intentionally left in place. Plan-04 defines `write_query_log_entry()` as the helper for cache-hit log entries; the fresh-query unconditional log remains as the existing inline code unless plan-04 explicitly moves it — check plan-04 before building.
- `result.cached_path` is a `Path` object (per the `QueryResult` schema in the spec). `str(result.cached_path)` produces a POSIX-style string on all platforms when the path was constructed from a vault-relative string like `queries/how-does-auth-work.md`. Use `result.cached_path.as_posix()` if cross-platform consistency is desired, but `str()` is consistent with how other path display is done in this file.
- The `from_cache` branch exits via `raise typer.Exit(code=0)` before reaching the fresh-query save prompt and inline log write — no `return` statement is used (consistent with the existing pattern in `_run_query_command()`).
