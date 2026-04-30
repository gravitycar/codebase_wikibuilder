# Critic Review: Implementation Plans

**Reviewed by**: Critic agent  
**Date**: 2026-04-30  
**Specification version**: v1.8.0  
**Plans reviewed**: 01–18 (all 18 implementation plans)

---

## Summary

All 18 implementation plans were reviewed against the specification (v1.8.0), the catalog, and each other. The plans are generally well-structured and detailed enough to build from. Most inter-plan dependency contracts are coherent.

**8 open questions** were raised, ranging from a definite logic bug (Q1) to design consistency questions (Q2–Q8). None block all plans from proceeding, but Q1, Q3, Q6, and Q7 must be resolved before the affected plans are built.

---

## Open Questions

### Q1 — BUG: Plan 16 `_most_recent_page()` lookup uses wrong key format (Task 77)

**Severity**: High (logic bug — function will always return None)

In `lint_dedup_healthcheck.py`, `_most_recent_page()` builds `entry_map` keyed by `entry.wikilink_target` (e.g. `"queries/how-does-auth-work"`) but looks up entries using `page.path.stem` (e.g. `"how-does-auth-work"`). The lookup will always miss. The dedup merge will silently use the wrong page as the canonical version.

**Fix needed**: Use `f"queries/{page.path.stem}"` as the lookup key, or key `entry_map` by stem only.

---

### Q2 — Plan 16/10: Private functions imported across modules (Task 78)

**Severity**: Medium (design clarity)

Plan 16 imports `_build_batches` and `_collect_summary_files` from `analysis.py` — both are underscore-prefixed private functions. Plan 16 notes these "should be promoted to public functions" but plan 10 does not reflect this decision.

**Fix needed**: Decide whether to rename these to `build_batches` / `collect_summary_files` in plan 10, and update plan 16's imports accordingly.

---

### Q3 — Plans 05/07/08/10: Vault constants duplicated in four modules (Task 79)

**Severity**: Medium (maintainability)

`_VAULT_SPECIAL_FILES` and `_VAULT_EXCLUDED_DIRS` are independently copied into `change_detector.py`, `vault.py`, `staleness.py`, and `analysis.py`. These should be defined once in `vault.py` and imported by the others.

**Fix needed**: Define constants in `vault.py` (plan 07) as public exports; update plans 05, 08, 10 to import from vault.

---

### Q4 — Plan 13: `LLMError` not caught on `LLMClient()` construction (Task 80)

**Severity**: Low–Medium (error handling consistency)

The `ingest` command (plan 10) wraps `LLMClient(config)` in a try/except catching `LLMError`. The `query` command (plan 13) does not. An invalid API key will produce an unhandled exception in `query` but a clean error message in `ingest`.

**Fix needed**: Add the same `LLMError` guard to plan 13's `query` command.

---

### Q5 — Plan 08: `detect_stale_queries()` signature differs from catalog (Task 82)

**Severity**: Low (documentation consistency)

The catalog specifies `detect_stale_queries(change_set, vault_root, log_fn, logger)` but plan 08 implements `detect_stale_queries(changed_vault_paths: set[str], vault_root, log_fn, logger)`. Plans 08 and 09 are internally consistent (plan 09 passes the pre-computed set), but the catalog is out of date.

**Fix needed**: Either update the catalog to reflect the actual signature, or revise plan 08 to accept the full `ChangeSet` and extract paths internally.

---

### Q6 — BUG: Plans 09/18: `try_enable_search_plugin()` never wired in any plan (Task 81)

**Severity**: High (feature silently never executes)

Plan 18 implements `try_enable_search_plugin(vault_root)` but delegates wiring to plan 09. Plan 09 does not call it — it only notes "a natural insertion point." The function will be dead code; the `--enable-obsidian-search` flag will have no effect.

**Fix needed**: Assign explicit ownership. Either plan 09 adds the call to `cli.py` (before `_run_phase1()`), or plan 18 adds a `cli.py` modification step.

---

### Q7 — Plan 11: `run_query()` raises `typer.Exit` — fragile MCP/CLI coupling (Task 83)

**Severity**: Medium (architectural coupling)

`run_query()` in `query_engine.py` is shared between the CLI (plan 13) and MCP server (plan 15). It raises `typer.Exit` (a `SystemExit` subclass) on error, which imports `typer` at runtime inside a library function. The MCP server must catch `SystemExit` to avoid process termination. Any future caller that forgets this catch will silently kill the process.

**Fix needed**: Decide whether `run_query()` should raise a domain exception (e.g. `QueryError`) instead, with the CLI and MCP server each translating to their own error mechanism. If `SystemExit`-based approach is accepted, document the requirement explicitly in both plan 11 and plan 15.

---

### Q8 — Plan 16: Dedup merge uses heuristic `_extract_first_prose` for description (Task 84)

**Severity**: Low (quality consistency)

All other vault pages have a `description:` field that is a one-line LLM-generated summary. The dedup merge pass generates the merged page's description using `_extract_first_prose()`, a heuristic prose extraction — which may produce multi-sentence or poorly formatted text.

**Fix needed**: Decide whether to add an LLM call during merge to generate a proper one-line description (consistent with all other pages), or explicitly accept the heuristic as a cost-saving trade-off.

---

## Plans with No Issues

Plans 01, 02, 03, 04, 06, 12, 14, 15, 17 were reviewed and found to be complete, unambiguous, and consistent with the specification and each other.

Plans 09, 10, 11, 13, 16, 18 each have at least one open question (see above).

---

## Verdict

The plans are sufficiently detailed to begin building. The 8 questions should be resolved before developers begin work on plans 08, 09, 10, 11, 13, 16, and 18. Plans 01–07, 12, 14, 15, 17 may proceed immediately.
