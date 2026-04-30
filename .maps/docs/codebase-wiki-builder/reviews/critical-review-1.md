# Critical Review #1: Codebase Wiki Builder Specification

**Reviewer**: Critic Agent (MAPS)
**Review Date**: 2026-04-27
**Spec Version**: 1.0.0
**Task ID**: 5

---

## Summary

The specification is well-structured and substantially complete for a greenfield CLI tool. It covers the problem statement, user story, stakeholders, success criteria, functional requirements, NFRs, constraints, technical context, data model, user workflows, acceptance tests, dependencies, risks, and out-of-scope items — matching the recommended template from the specification guidelines.

However, the review identified **10 open questions** requiring user resolution before implementation planning can proceed. Three of these were already flagged in the spec's own "Open Questions" section but were left unresolved; seven are new findings from this review.

---

## Checklist Results

### Completeness

| Criterion | Status | Notes |
|-----------|--------|-------|
| Clear problem statement | PASS | Executive summary and Context sections are clear |
| User story / stakeholder context | PASS | User story and Stakeholders sections present |
| Measurable acceptance criteria | PARTIAL | 10 acceptance tests present; test #2 uses unreliable timestamp comparison (see Q-14) |
| Functional requirements by capability | PASS | FR-1 through FR-7 well organized |
| Non-functional requirements | PARTIAL | Performance NFR uses unmeasurable language (see Q-11) |
| Explicit constraints (DO NOTs) | PASS | 12 explicit DO NOT constraints listed |
| Technical context | PASS | Stack, vault layout, and LLM abstraction defined |
| Out of scope section | PASS | 10 explicit out-of-scope items listed |
| Dependencies identified | PASS | Upstream, downstream, and external dependencies all present |

### Clarity

| Criterion | Status | Notes |
|-----------|--------|-------|
| Implementable without clarification | PARTIAL | Backlink detection methodology under-specified (see Q-9); query retrieval limits absent (see Q-10) |
| Active, specific language (SHALL/MUST) | PASS | Consistent use of SHALL throughout |
| No ambiguous terms | PARTIAL | "reasonable wall-clock time" in Performance NFR (see Q-11) |
| Edge cases and error scenarios covered | PARTIAL | Config validation errors not specified (see Q-13); empty-vault behavior for analysis/query not specified (see Q-15) |
| Given/When/Then format for workflows | PASS | 5 user workflow scenarios present |

### Specification Guidelines Compliance

| Criterion | Status | Notes |
|-----------|--------|-------|
| Specifies WHAT not HOW | PASS | No implementation-prescribing code examples in requirements |
| Includes "why" for non-obvious requirements | PASS | Rationale present for key constraints (e.g., DO NOT use OpenAI as primary) |
| References existing code patterns | N/A | Greenfield project — no existing patterns to reference |
| Under 10K tokens | PASS | Spec is approximately 4,000 words |

---

## Open Questions Found: 10

The following question tasks were created (all set to `in_progress` pending user resolution):

### Q-6: Summary file naming convention (BLOCKING)
The spec identifies two options — `<name>.<ext>.md` vs `<name>.md` — but does not choose. This affects vault layout, backlink path format, index entries, and change-detection regex. Must be resolved before implementation planning.

### Q-7: Analysis command fallback strategy when summaries exceed context window (BLOCKING)
Three options are listed in the spec (batch synthesis, index-only pass, truncate by recency) but none is chosen. Must be resolved before the `analysis` command can be planned.

### Q-8: Vault name config for multi-vault Obsidian plugin management
Since plugin management (FR-7) is already optional, option (c) — rely on active vault — may be the simplest MVP resolution. Low urgency but needs a decision.

### Q-9: Backlink detection methodology for dynamic/string-based references (BLOCKING)
FR-3.5 requires detecting "dynamic references (string-based, if detectable)" with no definition of what detection approach to use, what "detectable" means, or whether detection is language-specific. Implementers cannot write a consistent References section without this guidance.

### Q-10: Query command — retrieval limit and zero-results behavior (BLOCKING)
FR-5 places no upper bound on how many summaries may be passed to the LLM for answer generation. On large vaults this will exceed the context window. The zero-results edge case (LLM identifies no relevant summaries) is also undefined.

### Q-11: Performance NFR — "reasonable wall-clock time" is unmeasurable
The guidelines explicitly prohibit terms like "reasonable." The NFR as written is untestable. Recommend either removing it or replacing it with a measurable overhead budget (e.g., per-file processing overhead excluding LLM latency).

### Q-12: CLI exit codes not specified
The spec describes rich terminal output but never specifies exit codes. Partial success behavior (some files failed, others succeeded) is particularly ambiguous. This affects use in shell scripts and LLM-driven workflows.

### Q-13: Config validation on subsequent runs not specified
FR-2 describes first-run setup validation, but does not specify what happens when the config file exists but contains invalid values (stale path, malformed JSON, negative delay, deprecated model name).

### Q-14: Acceptance test #2 uses filesystem timestamps — unreliable verification method
"Timestamps changed" is not a reliable test oracle on fast hardware. The spec does not require preserving timestamps for unchanged files. Recommend replacing with LLM call-count verification or MD5 footer comparison.

### Q-15: `analysis` and `query` behavior when no summaries exist
FR-4 and FR-5 do not define behavior when the vault is empty (no summaries, no `index.md`). These are likely common first-run errors that need explicit error messages and exit behavior specified.

---

## Additional Observations (Not Open Questions)

The following items are observations that may inform the revision but do not require user decisions:

1. **`analysis` command: subset selection criterion is vague** — FR-4 says "a representative subset if the total exceeds context limits" but the selection algorithm is unresolved (this is captured more precisely in Q-7).

2. **`query` command LLM format for summary identification** — The spec says "use the LLM to identify which summary files are relevant" but does not specify the format in which the LLM should return this list (e.g., JSON array of paths, newline-delimited list). This could be left to the implementation plan, but it risks inconsistent parsing logic.

3. **`log.md` timestamp timezone** — The format `YYYY-MM-DD HH:MM:SS` is specified but the timezone (local vs UTC) is not. For a local single-user tool this is a minor point, but worth clarifying for reproducibility.

4. **`index.md` format: table vs list** — FR-3.6 says "`index.md` SHALL contain a table or list." Allowing either format means the acceptance test for `index.md` cannot verify structure — only non-emptiness. A single format should be mandated.

5. **The spec correctly identifies that the Obsidian CLI (v1.12.4+) is a recent addition** — the reference date in the spec (February 27, 2026) aligns with a feature released after the model's training cutoff. The implementation plan should treat Obsidian CLI integration as an exploratory component given limited available documentation.

---

## Verdict

The specification is above average quality and suitable for revision. Resolving the 4 BLOCKING questions (Q-6, Q-7, Q-9, Q-10) is the minimum required before implementation planning can proceed. The remaining 6 questions (Q-8, Q-11, Q-12, Q-13, Q-14, Q-15) improve testability and robustness but are lower priority.
