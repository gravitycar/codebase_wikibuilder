# Critic Review #1: Query Cache Specification

- **Review Task ID**: 113
- **Spec Version**: 1.0.0
- **Reviewed**: 2026-05-08
- **Reviewer**: Critic Agent (MAPS)

---

## Overall Assessment

The specification is well-structured and substantially complete. The problem statement, user story, functional requirements, non-functional requirements, constraints, technical context, and acceptance tests are all present. The spec is clear about the two-stage design, staleness handling, and integration points.

Nine open questions were identified — mostly implementation-level ambiguities and one significant structural gap (the public function signature of the cache module). None represent fundamental design problems; they are clarifications needed before a developer can build without making design decisions.

---

## Completeness Checklist

| Criterion | Status | Notes |
|-----------|--------|-------|
| Clear problem statement | PASS | Executive summary and Context section both present |
| User story | PASS | Present with primary and secondary stakeholders |
| Measurable acceptance criteria | PASS | 14 acceptance tests with specific verifiable behaviors |
| Functional requirements by capability | PASS | FR-QC-1 through FR-QC-9, well organized |
| Non-functional requirements | PASS | Latency budget, token cost, correctness |
| Explicit constraints (DO NOTs) | PASS | 10 explicit DO NOTs |
| Technical context | PASS | Relevant modules documented with key APIs |
| Out of scope section | PASS | 6 items listed |
| Dependencies identified | PASS | Upstream + downstream impact documented |

---

## Clarity Checklist

| Criterion | Status | Notes |
|-----------|--------|-------|
| Another developer could implement without clarification | PARTIAL | 9 open questions prevent this |
| Active, specific language (SHALL, MUST) | PASS | Consistent use throughout |
| No ambiguous terms without quantification | PASS | Latency target is quantified (50ms) |
| Edge cases and error scenarios covered | PARTIAL | Empty slug and numeric-suffix edge cases missing |
| Given/When/Then format for workflows | PASS | 5 scenarios with this format |

---

## Specification Guidelines Compliance

| Criterion | Status | Notes |
|-----------|--------|-------|
| Specifies WHAT, not HOW | PASS | No code examples; implementation left to developer |
| Includes "why" for non-obvious requirements | PASS | Conservative LLM bias rationale, TTL exclusion rationale |
| References existing code patterns | PASS | Extensive module-level references in Technical Context |
| Under 10K tokens | PASS | Estimated ~4K tokens |

---

## Open Questions Found

### Q-1 (Task #114): Stage 1 normalization vs. slugify() double-processing

**Severity**: HIGH — could cause systematic cache misses

FR-QC-2 Stage 1 instructs: (1) normalize the incoming question, then (2) apply `slugify()` to the normalized question. But `_make_slug()` in `query_persistence.py` calls `slugify(question)` directly on the raw question text (no pre-normalization). If Stage 1 pre-normalizes before slugifying, the lookup slug will differ from the saved slug, breaking Stage 1 entirely for any question with punctuation.

The normalization step appears to belong only to the comparison in step 4b, not to the slug generation in step 2.

---

### Q-2 (Task #115): Stage 2 numeric-suffix files not addressed for LLM-returned path

**Severity**: MEDIUM — edge case that could cause misses or incorrect behavior

FR-QC-2 Stage 2 step 5a resolves the LLM-returned wikilink path to a single file by appending `.md`. It does not address: (a) whether numeric-suffix wikilinks like `queries/how-does-auth-work-2` can be returned by the LLM (they can, since they appear in `index.md`), and (b) whether Stage 2 should try numeric-suffix siblings if the LLM-returned file is stale (current spec says "do not try further candidates" — is this intentional?).

---

### Q-3 (Task #116): one_line_summary source for numeric-suffix cache hits

**Severity**: MEDIUM — behavioral gap for valid real-world scenario

FR-QC-4 says `one_line_summary` comes from the matching `index.md` row. When the matched file is a numeric-suffix variant (e.g., `queries/how-does-auth-work-2.md`), the correct wikilink key to look up in `index.md` is `queries/how-does-auth-work-2`. The spec does not confirm this lookup key or specify a fallback if the row is missing.

---

### Q-4 (Task #117): Use of private functions `_has_stale_banner` and `_parse_existing_index` across modules

**Severity**: MEDIUM — design/encapsulation concern

The spec mandates that `query_cache.py` call `staleness._has_stale_banner()` and `index_writer._parse_existing_index()`, both of which are private (`_`-prefixed) functions. Cross-module calls to private functions violate encapsulation and create fragile coupling. The spec should specify whether these functions should be made public as part of this feature.

---

### Q-5 (Task #118): Empty slug fallback not specified for Stage 1 lookup

**Severity**: LOW — edge case, but should match save behavior

`slugify()` returns `""` for questions consisting entirely of non-alphanumeric characters. `_make_slug()` applies a `"query"` fallback in this case. FR-QC-2 Stage 1 does not specify whether Stage 1 should apply the same fallback or skip directly to Stage 2.

---

### Q-6 (Task #119): log.md entry format and caller responsibility for cache hits

**Severity**: MEDIUM — behavioral gap for MCP path

FR-QC-6 says CLI cache hits still write a `query` log entry, but only covers CLI behavior. The spec does not specify whether the MCP server writes a `query` log entry (for cache hits or fresh results). The MCP server's existing flow only shows `save_query_page()` being called, which writes `query-saved`. If the MCP never wrote a `query` log entry before, it shouldn't be expected to start now — but the spec's silence on this is ambiguous.

---

### Q-7 (Task #120): cache_hit/cached_at fields absent from fresh MCP response — schema enforcement

**Severity**: LOW — spec is directionally clear, but wording should be strengthened

FR-QC-7 states `cache_hit` shall be `false` and `cached_at` shall be `null` on fresh responses. This is correct and verified by AT-9. However, the spec should explicitly state that ALL 6 fields in the updated schema are ALWAYS present in every `wiki_query` response (not conditionally included), to prevent an implementer from only adding these fields on cache hits.

---

### Q-8 (Task #121): Cache module public function signature not specified

**Severity**: HIGH — implementer cannot write the integration point without this

FR-QC-1 says the module exposes "a single public function" but never names it, specifies its parameters, or defines its return type. An implementer cannot write `run_query()`'s integration point (FR-QC-5) without knowing: the function name, whether it receives `index_content` as a pre-read string or reads it itself, whether it receives `stale_warnings` as a parameter, and whether it returns `QueryResult | None` or something else.

---

### Q-9 (Task #122): answer field reconstruction — sources section format unspecified

**Severity**: MEDIUM — implementer must guess or inspect source code

FR-QC-4 says `answer` for cache hits is "the full answer body from the saved page (including the `## Sources` section)." But `QueryPage.answer_body` does not include the `## Sources` section — it must be reconstructed. The spec says to use "same format as a freshly generated answer" but never documents what that format is. The implementer would need to inspect `query_engine.py` or `save_query_page()` to find the actual format, rather than being able to build from the spec alone.

---

## Summary

| # | Task ID | Title | Severity |
|---|---------|-------|----------|
| Q-1 | 114 | Stage 1 normalization vs. slugify() double-processing | HIGH |
| Q-2 | 115 | Stage 2 numeric-suffix files not addressed for LLM-returned path | MEDIUM |
| Q-3 | 116 | one_line_summary source for numeric-suffix cache hits | MEDIUM |
| Q-4 | 117 | Use of private functions _has_stale_banner and _parse_existing_index across modules | MEDIUM |
| Q-5 | 118 | Empty slug fallback not specified for Stage 1 lookup | LOW |
| Q-6 | 119 | log.md entry format and caller responsibility for cache hits | MEDIUM |
| Q-7 | 120 | cache_hit/cached_at fields absent from fresh MCP response — schema enforcement | LOW |
| Q-8 | 121 | Cache module public function signature not specified | HIGH |
| Q-9 | 122 | answer field reconstruction — sources section format unspecified | MEDIUM |

**Total open questions**: 9  
**HIGH severity**: 2 (Q-1, Q-8)  
**MEDIUM severity**: 5 (Q-2, Q-3, Q-4, Q-6, Q-9)  
**LOW severity**: 2 (Q-5, Q-7)

The two HIGH severity issues (Q-1 slug double-processing and Q-8 missing function signature) should be resolved before implementation begins, as they are prerequisites for a correct and buildable implementation.
