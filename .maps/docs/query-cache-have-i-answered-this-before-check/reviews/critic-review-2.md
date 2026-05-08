# Critic Review #2: Query Cache Specification

- **Review Task ID**: 125
- **Spec Version**: 1.1.0
- **Reviewed**: 2026-05-08
- **Reviewer**: Critic Agent (MAPS)

---

## Overall Assessment

Spec v1.1.0 correctly and completely incorporates all 9 Round 1 Q&A resolutions. The two HIGH-severity issues (Q-1 slug double-processing, Q-8 missing function signature) are cleanly resolved. No Round 1 question is partially or incorrectly incorporated.

Four new gaps were found. Two are HIGH severity: a missing data carrier in `QueryResult` that prevents the CLI and MCP from accessing attribution data they are required to display, and a log-writing mechanism gap for the MCP cache-hit path. The other two are MEDIUM severity: a private function encapsulation gap and an internal specification contradiction about what data is included in the Stage 2 LLM prompt.

---

## Round 1 Q&A Incorporation Check

| Q# | Task ID | Title | Incorporated? | Notes |
|----|---------|-------|---------------|-------|
| Q-1 | 114 | Stage 1 normalization vs. slugify() double-processing | YES | FR-QC-2 Stage 1 step 1 and Technical Context normalization section both correct |
| Q-2 | 115 | Stage 2 numeric-suffix files / sibling fallback | YES | FR-QC-2 Stage 2 step 5a–5d address both sub-issues cleanly |
| Q-3 | 116 | one_line_summary source for numeric-suffix cache hits | YES | index.md wikilinks include numeric suffixes; lookup key is the full wikilink path |
| Q-4 | 117 | Private `_has_stale_banner` cross-module call | YES | Renamed to `has_stale_banner` (public); FR-QC-3 explicitly labels it a public function |
| Q-5 | 118 | Empty slug fallback not specified for Stage 1 | YES | FR-QC-2 Stage 1 step 2 explicitly skips Stage 1 with no fallback |
| Q-6 | 119 | log.md entry for MCP cache hits | YES | FR-QC-7 step 6 added; DO NOTs section updated |
| Q-7 | 120 | All 6 MCP fields always present | YES | FR-QC-7 schema block and Data Requirements table both updated |
| Q-8 | 121 | Cache module function signature not specified | YES | FR-QC-1 now specifies full signature with all parameters and return type |
| Q-9 | 122 | answer field reconstruction — sources section format | YES | FR-QC-4 and Technical Context "Reconstructing QueryResult from Cache" specify verbatim raw content |

All 9 prior questions are fully and correctly incorporated.

---

## New Gaps Found

### NQ-1 (Task #127): QueryResult missing cached_path and cached_at fields — HIGH severity

**The gap**: FR-QC-6 step 1 requires the CLI to print:
```
[cache] Answering from saved page: <vault-relative path> (saved <saved_at timestamp>)
```
FR-QC-7 steps 3–4 require the MCP response to include `cached_at` (the `saved_at` timestamp) and `saved_path` (the vault-relative path of the matched page).

FR-QC-4 specifies only ONE new field on `QueryResult`: `from_cache: bool`. The matched page's vault-relative path and `saved_at` timestamp are available inside `check_query_cache()` but there is no defined path for these values to flow through `QueryResult` to the CLI or MCP callers.

FR-QC-4 explicitly states "No other existing fields are added or removed" — which would prohibit the obvious fix. The spec needs to either:
- Add `cached_path: str | None = None` and `cached_at: str | None = None` fields to `QueryResult` and update FR-QC-4 accordingly, OR
- Specify an alternative mechanism for the callers to access these values.

**Impact**: Without this resolved, neither the CLI attribution line nor the MCP `cached_at`/`saved_path` fields can be implemented per spec.

---

### NQ-2 (Task #128): MCP log.md write on cache hit — mechanism unspecified — HIGH severity

**The gap**: FR-QC-7 step 6 requires the MCP server to write a `query` log entry for cache hits. In the existing MCP flow, the only log-writing mechanism is inside `save_query_page()` (which writes `query-saved`). On a cache hit, `save_query_page()` is skipped. No other log-writing path exists in the MCP handler.

The spec does not specify:
1. How `_handle_wiki_query()` writes the log entry when `save_query_page()` is skipped (no utility function, no `log_fn` parameter, no alternative call defined).
2. The exact entry type/format. The spec says "the same as for fresh queries, with a `cache-hit` marker" — but fresh MCP queries write `query-saved`, not `query`. The CLI writes `query`. These are different.

**Impact**: The implementer cannot write the MCP log entry without guessing at both the mechanism and the format.

---

### NQ-3 (Task #129): `_parse_existing_index()` still private — MEDIUM severity

**The gap**: Q-4 from Round 1 flagged both `_has_stale_banner` and `_parse_existing_index` as private functions called cross-module. The spec resolved `_has_stale_banner` by making it public. However, `_parse_existing_index()` in `index_writer.py` remains `_`-prefixed and is still referenced in the Technical Context as the mechanism for Stage 2 index parsing. FR-QC-2 Stage 2 step 1 depends on it.

Calling a private function from `query_cache.py` creates the same fragile cross-module coupling that drove Q-4. The spec should either rename `_parse_existing_index()` to `parse_existing_index()` (public) consistent with the `has_stale_banner` precedent, or specify an alternative approach.

**Impact**: The implementer faces a design decision the spec should have resolved (same as Q-4 did for `_has_stale_banner`).

---

### NQ-4 (Task #130): Stage 2 prompt inconsistency — "H1 questions" vs. OQ-1 deferral — MEDIUM severity

**The gap**: FR-QC-2 Stage 2 step 3 states (normatively, using SHALL context): "Construct a prompt instructing the LLM to compare the incoming question against the list of existing query page **titles (H1 questions)** and their one-line descriptions."

The phrase "H1 questions" implies H1 titles are always read from the saved pages. But Open Question OQ-1 (retained in v1.1.0) explicitly defers this choice to the implementer: either de-slugify the wikilink path (no file reads) or read actual H1 titles (requires file I/O per page).

This is a direct contradiction:
- Step 3 implies H1 reads are mandatory (normative language)
- OQ-1 says the choice is left to the implementer

If option (a) is implemented (de-slugified slug proxy), step 3's wording is wrong. If option (b) is always required, OQ-1 should be closed as a normative requirement, not left open.

**Impact**: The implementer cannot determine what the Stage 2 prompt should contain. Acceptance tests AT-5 and AT-6 also cannot be written unambiguously until this is resolved.

---

## Completeness Checklist (v1.1.0)

| Criterion | Status | Notes |
|-----------|--------|-------|
| Clear problem statement | PASS | Unchanged from v1.0.0 |
| User story | PASS | Unchanged |
| Measurable acceptance criteria | PASS | 14 acceptance tests |
| Functional requirements by capability | PASS | FR-QC-1 through FR-QC-9 |
| Non-functional requirements | PASS | Latency budget, token cost, correctness |
| Explicit constraints (DO NOTs) | PASS | Updated with cache-hit log entry DO NOT |
| Technical context | PASS | Extensive module-level references |
| Out of scope section | PASS | Unchanged |
| Dependencies identified | PASS | Upstream + downstream impact documented |
| Another developer could implement without clarification | FAIL | NQ-1 and NQ-2 are blockers |
| Active, specific language (SHALL, MUST) | PASS | Consistent |
| No ambiguous terms | PARTIAL | NQ-4 contradiction in Stage 2 prompt language |
| Edge cases and error scenarios covered | PASS | Error handling, empty slug, stale bypass all specified |

---

## Summary

| # | Task ID | Title | Severity |
|---|---------|-------|----------|
| NQ-1 | 127 | QueryResult missing cached_path and cached_at fields | HIGH |
| NQ-2 | 128 | MCP log.md write mechanism unspecified for cache hits | HIGH |
| NQ-3 | 129 | _parse_existing_index() still private — same issue as Q-4 for _has_stale_banner | MEDIUM |
| NQ-4 | 130 | Stage 2 prompt "H1 questions" contradicts OQ-1 deferral | MEDIUM |

**Total new questions**: 4
**HIGH severity**: 2 (NQ-1, NQ-2)
**MEDIUM severity**: 2 (NQ-3, NQ-4)

NQ-1 and NQ-2 are implementation blockers. Without resolving NQ-1, neither the CLI attribution line nor the MCP `cached_at`/`saved_path` fields can be built. Without resolving NQ-2, the MCP log-writing requirement cannot be implemented.
