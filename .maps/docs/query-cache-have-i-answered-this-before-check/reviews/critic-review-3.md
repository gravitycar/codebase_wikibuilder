# Critic Review #3: Query Cache Specification v1.2.0

- **Review Task ID**: 133
- **Spec Version**: 1.2.0
- **Reviewed**: 2026-05-08
- **Reviewer**: Critic Agent (MAPS)
- **Round**: Final (Round 3 of 3)

---

## Overall Assessment

Spec v1.2.0 correctly and completely incorporates all 4 Round 2 Q&A resolutions (NQ-1 through NQ-4). No Round 2 question is partially or incorrectly incorporated. The spec is substantially implementation-ready.

Three minor observations were identified — all LOW severity. None block implementation or require user clarification before code is written. No new question tasks were created.

**Verdict: READY FOR SIGN-OFF.**

---

## Round 2 Q&A Incorporation Check

| Q# | Task ID | Title | Incorporated? | Notes |
|----|---------|-------|---------------|-------|
| NQ-1 | 127 | QueryResult missing `cached_path` and `cached_at` fields | YES | FR-QC-4 now defines both fields with correct types (`Path \| None`, `str \| None`); Data Requirements table updated |
| NQ-2 | 128 | MCP log.md write mechanism unspecified for cache hits | YES | FR-QC-7a defines `write_query_log_entry()` helper in `query_persistence.py`; FR-QC-6 step 5 and FR-QC-7 step 6 both reference it |
| NQ-3 | 129 | `_parse_existing_index()` still private | YES | Renamed to `parse_existing_index()` (public) per version history; Technical Context references it as public |
| NQ-4 | 130 | Stage 2 prompt "H1 questions" contradicts OQ-1 deferral | YES | FR-QC-2 Stage 2 step 3 now normatively requires reading actual H1 titles from disk via `read_query_page()`; OQ-1 is removed from Open Questions |

All 4 prior questions are fully and correctly incorporated.

---

## Completeness Checklist (v1.2.0)

| Criterion | Status | Notes |
|-----------|--------|-------|
| Clear problem statement | PASS | Unchanged; executive summary and Context section both strong |
| User story | PASS | Unchanged |
| Measurable acceptance criteria | PASS | 14 acceptance tests with specific verifiable behaviors |
| Functional requirements by capability | PASS | FR-QC-1 through FR-QC-9 plus FR-QC-7a |
| Non-functional requirements | PASS | Latency budget (50ms Stage 1), token cost, correctness |
| Explicit constraints (DO NOTs) | PASS | 10 explicit constraints including cache-hit log requirement |
| Technical context | PASS | Module-level references updated; Reconstructing QueryResult section accurate |
| Out of scope section | PASS | Unchanged |
| Dependencies identified | PASS | Upstream and downstream impact documented |
| Another developer could implement without clarification | PASS | Three minor observations noted below; none are blockers |
| Active, specific language (SHALL, MUST) | PASS | Consistent throughout |
| No ambiguous terms | PASS | All quantified or resolved |
| Edge cases and error scenarios covered | PASS | Empty slug, numeric suffix, stale bypass, LLM error, parse error all covered |

---

## Observations (Non-Blocking)

These are minor clarity issues that do not require user resolution before implementation. They are documented for the implementer's awareness.

### OBS-1: `log_fn: ...` is not a valid type annotation in FR-QC-7a

**Location**: FR-QC-7a function signature

**Issue**: The parameter type for `log_fn` is written as `...` (ellipsis), which is not a valid Python type annotation. The intended type is `Callable[[str], None]`, consistent with how `log_fn` is typed throughout the codebase (e.g., in `save_query_page()`).

**Impact**: None in practice. An implementer familiar with the codebase will recognize the correct type immediately. The signature is purely illustrative.

**Recommendation**: The implementer should use `Callable[[str], None]` for the type annotation. No spec revision required.

---

### OBS-2: FR-QC-5 step 3 implies `run_query()` sets `from_cache=True`, contradicting FR-QC-1 and FR-QC-4

**Location**: FR-QC-5 step 3 vs. FR-QC-1 and FR-QC-4

**Issue**: FR-QC-5 step 3 says "Call the cache lookup function. If it returns a valid `QueryResult`, **set `from_cache=True` on the result** and return it immediately." This implies `run_query()` sets the field after receiving the result. However, FR-QC-1 states the function "Returns a `QueryResult` with `from_cache=True` on a cache hit" and FR-QC-4 defines `from_cache` as set by `check_query_cache()` on a hit.

**Impact**: Minor. Both approaches produce the same observable behavior. An implementer who follows FR-QC-1 and FR-QC-4 (sets `from_cache=True` inside `check_query_cache()`) is correct. FR-QC-5's phrasing is slightly imprecise.

**Recommendation**: Follow FR-QC-1 and FR-QC-4: `check_query_cache()` returns the result with `from_cache=True` already set. The `run_query()` integration point simply checks `if result is not None` and returns it. No spec revision required.

---

### OBS-3: `cache-hit` log entry marker format is not specified

**Location**: FR-QC-6 step 5, FR-QC-7 step 6, FR-QC-7a

**Issue**: The spec says cache-hit log entries include "a `cache-hit` marker" but never shows the exact format of the marker or the full log entry structure. The existing `query-saved` and `query` entry formats from `query_persistence.py` are also not documented in the spec.

**Impact**: Low. The implementer must inspect `query_persistence.py` to understand the existing log entry format, then decide where to insert the `cache-hit` marker. This is a minor implementation detail that does not affect correctness of the feature from the caller's perspective.

**Recommendation**: The implementer should follow the existing `query` log entry format from `query_persistence.py` and append `| cache-hit` (or equivalent) as a trailing marker field. Acceptance tests AT-1 and AT-8 only verify that a log entry EXISTS — they do not validate its exact format — so any reasonable format passes.

---

## Specification Integrity Check

No contradictions with the `Out of Scope` section. No DO NOT constraints are violated by any requirement. The three open questions retained in the spec (OQ-1 removed; the remaining open questions in the spec concern Stage 2 prompt conservatism, `saved_at` parsing fallback, and model selection) are appropriately flagged as implementer guidance, not unresolved blockers.

The acceptance tests (AT-1 through AT-14) are complete and consistent with the functional requirements. Each test is unambiguously verifiable via mock assertions on `LLMClient.complete()` and filesystem checks.

---

## Summary

| Category | Count |
|----------|-------|
| Round 2 questions correctly incorporated | 4 of 4 |
| New question tasks created | 0 |
| Non-blocking observations | 3 |
| Implementation blockers remaining | 0 |

**The specification is clean and ready for implementation planning sign-off.**
