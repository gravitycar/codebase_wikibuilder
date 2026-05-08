# Critic Review: Implementation Plans
## Query Cache — Have I Answered This Before?

- **Review type**: Critical Review #3 (Implementation Plans)
- **Spec version reviewed**: v1.4.0
- **Plans reviewed**: plan-01 through plan-08
- **Date**: 2026-05-08
- **Reviewer**: Critic Agent (MAPS)

---

## Overall Verdict

The 8 plans collectively cover all spec requirements and acceptance tests. Dependency ordering
is correct. Three issues require clarification before implementation begins; they are not
blocking in the sense of missing entire features, but they create ambiguity that will cause
bugs or wasted effort if left unresolved.

---

## Plan-by-Plan Assessment

### plan-01 — Promote has_stale_banner to Public API
**Status: CLEAN**

Pure rename. All call sites identified (lines 181, 342 in `staleness.py`; lines 14, 86, 90, 96
in `test_staleness.py`). No logic changes. No new tests needed. Buildable as written.

### plan-02 — Promote parse_existing_index to Public API
**Status: CLEAN**

Pure rename. All call sites identified (line 40 in `index_writer.py`; lines 127, 139, 149 in
`test_index_writer.py`). No logic changes. No new tests needed. Buildable as written.

### plan-03 — Extend QueryResult with Cache Fields
**Status: CLEAN with minor labeling issue**

The dataclass extension is correct and backward-compatible. Field ordering is valid. Test class
`TestQueryResultCacheFields` is comprehensive (8 test cases covering all combinations).

**Minor issue**: The Dependencies section says "Blocks: plan-04 (query_cache module)". plan-04 is
the log helper; it does NOT depend on the `QueryResult` cache fields. plan-05 is the cache module
that blocks on plan-03. The label "plan-04" should read "plan-05". This is a documentation error
only — it does not affect implementation order since the real build order (01 → 02 → 03 → 05 →
06 → [07, 08]) is correctly stated in each plan's own Dependencies section.

### plan-04 — Extract write_query_log_entry Helper
**Status: GAP — double log entry for fresh saved queries**

The plan replaces `_write_log_entry()` inside `save_query_page()` with a call to
`write_query_log_entry()`. This is correct for the cache-hit path.

**Issue**: `save_query_page()` now writes a log entry (via `write_query_log_entry()` with
`cache_hit=False`) every time it is called. In `_run_query_command()` in `cli.py`, plan-07
explicitly leaves the existing inline log write in place:

```python
# Step 10 in CLI (plan-07, fresh-query path — unchanged)
ts = datetime.now(tz=timezone.utc).strftime(...)
log_fn(f"{ts} | query | {question} | sources: {sources_summary}")
```

When a user runs a fresh query and chooses to save, the sequence is:
1. `save_query_page()` → calls `write_query_log_entry()` → writes `query-saved` log entry
2. CLI step 10 → writes a second `query | ...` log entry

Two entries are written to `log.md` for a single saved fresh query. This is a regression
from the existing behaviour (one entry per query). Plan-07 notes this ambiguity ("verify
against plan-04 before building") but does not resolve it.

See question **PQ-1**.

### plan-05 — Implement query_cache.py (Core Cache Module)
**Status: CLEAN**

The two-stage implementation is complete and correct. All SEC-3 checks are implemented
(`_validate_stage2_path`). Error handling follows FR-QC-9. The circular import avoidance
strategy (deferred `QueryResult` import inside function bodies) is correctly explained.

The `_collect_stale_warnings_from_content()` duplication from `query_engine.py` is
intentional and acknowledged. The `parse_existing_index(vault_root)` re-read of `index.md`
(when `index_content` is also available) is acknowledged in Note 2 and is not a correctness
issue.

The `_parse_stage2_response()` function takes the first token of the LLM response and
applies `rstrip(".,;:")`. This correctly handles trailing punctuation. Since slugs never
contain spaces, the space-split approach is safe.

Test coverage in `TestCheckQueryCache` is comprehensive and maps directly to acceptance
tests AT-1 through AT-17.

### plan-06 — Integrate Cache Pre-Check into run_query()
**Status: CLEAN**

Minimal and correct. The `cache_result.stale_warnings = stale_warnings` overwrite
pattern is sound — it ensures the authoritative stale warnings from `run_query()`'s own
scan are used, not the internally re-derived copy inside `check_query_cache()`. The
mutation is safe because `QueryResult` is an unfrozen dataclass.

Circular import analysis is correct: `query_engine` → `query_cache` (module level),
`query_cache` → `query_engine` only inside function bodies (deferred). No cycle.

Tests in `TestRunQueryCacheIntegration` cover AT-1, AT-14, FR-QC-8, and argument
forwarding.

### plan-07 — Update CLI for Cache Hit Attribution
**Status: GAP — attribution line format contradicts AT-1**

The plan contains an explicit discrepancy between the spec and its own build instructions:

**Spec FR-QC-6.1** and **AT-1(a)** specify:
```
[cache] Answering from saved page: queries/how-does-auth-work.md (saved 2026-04-29 10:00:00 UTC)
```

**Plan-07 build instruction** uses:
```
[cache] Answering from queries/how-does-auth-work.md, saved 2026-04-29 10:00:00 UTC
```

The plan notes: "Follow the task description format exactly, as it is the direct build
instruction." However, AT-1(a) in the spec reads: "the CLI output begins with a `[cache]
Answering from saved page: queries/how-does-auth-work.md` line". A test written against AT-1
will assert the `saved page:` wording. A developer building to the plan-07 format will fail AT-1.

This is a direct contradiction between a plan and an acceptance test. It must be resolved
before implementation.

See question **PQ-2**.

Also inherits the double-log issue from plan-04 (PQ-1).

### plan-08 — Update MCP Handler for Cache Hit Response
**Status: QUESTION — sources field removal is an undocumented breaking change**

The plan correctly implements all FR-QC-7 requirements: `from_cache` branching,
`write_query_log_entry()` for cache-hit logs, all 6 fields in every response.

**Issue**: Note 2 and Note 6 state that the `sources` field is **removed** from the MCP
JSON response as part of the transition to the 6-field schema. The current MCP response
includes `sources`. The spec's 6-field schema (FR-QC-7) does not list `sources`, but the
spec also says "No existing fields are removed" in the `QueryResult` schema table (FR-QC-4)
— though that refers to `QueryResult` not the MCP response.

The spec's MCP response change table (Data Requirements section) marks `source` as absent
from the new 6-field schema. However, the spec does not explicitly say "remove `sources`"
— it only specifies what the 6 fields SHALL be. The plan interprets the absence of `sources`
from the 6-field list as an implicit removal. Any existing MCP caller that reads `sources`
will break silently.

See question **PQ-3**.

---

## Cross-Plan Consistency Check

### Dependency Ordering

| Plan | Depends On | Stated In Plan | Correct? |
|------|-----------|----------------|---------|
| plan-01 | Nothing | Correctly stated | Yes |
| plan-02 | Nothing | Correctly stated | Yes |
| plan-03 | Nothing | Correctly stated | Yes |
| plan-04 | Nothing | Correctly stated | Yes |
| plan-05 | plan-01, 02, 03 | Correctly stated | Yes |
| plan-06 | plan-03, 05 | Correctly stated | Yes |
| plan-07 | plan-04, 06 | Correctly stated | Yes |
| plan-08 | plan-03, 04, 06 | Correctly stated | Yes |

### Spec Requirements Coverage

| Requirement | Covered By | Status |
|-------------|-----------|--------|
| FR-QC-1 (Cache module, check_query_cache signature) | plan-05 | Covered |
| FR-QC-2 Stage 1 (slug walk) | plan-05 | Covered |
| FR-QC-2 Stage 2 (LLM pre-check) | plan-05 | Covered |
| FR-QC-2 SEC-3 path validation | plan-05 | Covered |
| FR-QC-3 (Staleness check) | plan-05 | Covered |
| FR-QC-4 (QueryResult fields) | plan-03, plan-05 | Covered |
| FR-QC-5 (Integration point in run_query) | plan-06 | Covered |
| FR-QC-6 (CLI cache hit behaviour) | plan-07 | Covered (with PQ-2 caveat) |
| FR-QC-7 (MCP cache hit behaviour) | plan-08 | Covered (with PQ-3 caveat) |
| FR-QC-7a (write_query_log_entry helper) | plan-04 | Covered (with PQ-1 caveat) |
| FR-QC-8 (Cache miss fallthrough) | plan-05, plan-06 | Covered |
| FR-QC-9 (Error handling) | plan-05 | Covered |
| has_stale_banner public API | plan-01 | Covered |
| parse_existing_index public API | plan-02 | Covered |

### Acceptance Test Coverage

| AT | Covered By | Notes |
|----|-----------|-------|
| AT-1 | plan-05, 06, 07 | PQ-2: format mismatch |
| AT-2 | plan-05 | Covered |
| AT-3 | plan-05 | Covered |
| AT-4 | plan-05 | Covered |
| AT-5 | plan-05, 06 | Covered |
| AT-6 | plan-05, 06 | Covered |
| AT-7 | plan-05, 06 | Covered |
| AT-8 | plan-05, 06, 08 | Covered |
| AT-9 | plan-08 | Covered |
| AT-10 | plan-03 | Covered |
| AT-11 | plan-05 | Covered |
| AT-12 | plan-05 | Covered |
| AT-13 | plan-07 | Covered |
| AT-14 | plan-06, 07 | Covered |
| AT-15 | plan-05 | Covered |
| AT-16 | plan-05 | Covered |
| AT-17 | plan-05 | Covered |

---

## Questions Created

- **PQ-1** (task created): Double log entry for fresh saved queries in CLI
- **PQ-2** (task created): CLI attribution line format contradiction with AT-1
- **PQ-3** (task created): MCP sources field removal — implicit or explicit?

---

## Conclusion

All 8 plans are complete, internally consistent with each other, and collectively cover all 17
acceptance tests and all functional requirements. Three cross-plan issues require user
clarification (PQ-1, PQ-2, PQ-3) before a developer can build without making an unguided
design decision. No plan is missing a required function, file, or test class. Dependency
ordering is correct throughout.
