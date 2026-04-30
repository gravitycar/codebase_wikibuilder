# Critical Review #3: Codebase Wiki Builder Specification (v1.7.0)

**Reviewer**: Critic Agent (MAPS)
**Review Date**: 2026-04-29
**Spec Version**: 1.7.0
**Task ID**: 29

---

## Summary

This review focuses on features added since Critical Review #2: query answer persistence (v1.3), lint command + staleness nagging (v1.4), MCP server + help command (v1.5), MCP wiki_query auto-save behavior (v1.6), and the CLI rename to `codewiki` (v1.7). The v1.7.0 rename is a mechanical text substitution with no behavioral changes and introduces no new issues. The prior features (v1.3–v1.6) introduce **12 open questions**, ranging from blocking implementation gaps to testability defects.

The specification has grown substantially and maintains high structural quality. However, the new features introduced several under-specified edge cases, one logical impossibility (FR-8.3 asks the LLM to detect contradictions from titles-only context), and multiple ambiguities in the staleness detection and deduplication flows that would cause non-deterministic behavior across implementations.

---

## Prior Question Verification

All 3 questions from Critical Review #2 are verified as properly incorporated:

| Question | Subject | Status | Evidence |
|----------|---------|--------|----------|
| NQ-1 | Context window constants — hardcoded values | PASS | `ANALYSIS_CONTEXT_WINDOW = 64_000` and `QUERY_CONTEXT_WINDOW = 128_000` added to Technical Context and FR-4/FR-5 |
| NQ-2 | Query citation format — trailing `## Sources` section | PASS | FR-5 mandates `## Sources` trailing section with markdown list; acceptance test #7 updated |
| NQ-3 | Oversized single summary + budget overflow truncation | PASS | FR-5 specifies skip-and-annotate for oversized files, "X additional relevant files" note for truncation; acceptance tests #11 and #12 added |

---

## Checklist Results (v1.7.0, new features only)

### Completeness

| Criterion | Status | Notes |
|-----------|--------|-------|
| FR-3.8 staleness detection complete | PARTIAL | Change set timing with respect to deletions unspecified (Q-39); banner detection/deduplication rule unspecified (Q-31) |
| FR-8.1 lint staleness resolution complete | PARTIAL | Zero-relevant-files edge case unhandled (Q-33); log.md write ambiguity (Q-40) |
| FR-8.2 deduplication complete | PARTIAL | "Most recently saved" determination unspecified (Q-34) |
| FR-8.3 deep health-check complete | FAIL | LLM cannot detect contradictions or concept gaps from `index.md` alone — logical impossibility (Q-35) |
| FR-9.2 MCP tool schema complete | PARTIAL | `stale_warning` format unspecified (Q-36) |
| FR-10 help command complete | PARTIAL | Unknown-argument error behavior undefined (Q-37) |
| FR-5 query save complete | PARTIAL | `index.md` description generation mechanism unspecified (Q-41) |

### Clarity

| Criterion | Status | Notes |
|-----------|--------|-------|
| Stale banner placement | FAIL | Contradicts H1 title requirement; position not specified (Q-30) |
| Stale banner update-in-place | FAIL | Detection mechanism not specified — non-deterministic (Q-31) |
| Sources section parsing robustness | PARTIAL | Missing/malformed Sources section behavior undefined (Q-32) |
| Change set construction timing | FAIL | Ambiguous whether deleted files are in change set (Q-39) |
| Acceptance test numbering | FAIL | Test #19 appears out of sequence in document (Q-38) |

### Specification Guidelines Compliance

| Criterion | Status | Notes |
|-----------|--------|-------|
| Specifies WHAT not HOW | PASS | No prescriptive implementation details in new features |
| Includes "why" for non-obvious requirements | PASS | FR-9.2 rationale for always-saving MCP is well-explained; FR-8.2 preamble clearly explains deduplication motivation |
| Under 10K tokens | PASS | Spec is approximately 7,000 words |

---

## Open Questions Found: 12

All 12 question tasks have been created as children of task #29 and set to `in_progress`.

### Q-30 (BLOCKING): Stale banner placement conflicts with mandatory H1 title — Task #30

**Section**: FR-3.8 step 3a, FR-5, FR-8.1 step 3a

FR-3.8 says the stale banner goes "at the very top of the query page file." FR-5 requires the first line of every saved query page to be the original question as an `# H1` title. These two requirements are mutually exclusive. Additionally, FR-8.1's regeneration logic reads "the page's H1 heading" to recover the original question — this only works if the H1 is findable regardless of banner presence. The spec must specify whether the banner goes before the H1, after the H1, or after the H1 and a blank line.

---

### Q-31 (BLOCKING): No mechanism specified for detecting an existing stale banner — Task #31

**Section**: FR-3.8 step 3a

The rule "do not duplicate the banner" requires the application to detect a pre-existing banner. No detection approach is specified. Without a deterministic rule, two consecutive ingest runs on the same stale page could produce duplicate banners. The spec must define: (a) the exact detection pattern (fixed position vs. full-file scan), and (b) what "update in place" means — replace the entire callout block or just the changed-files line.

---

### Q-32: FR-3.8 staleness check behavior when Sources section is missing or malformed — Task #32

**Section**: FR-3.8 step 2

The spec defines the Sources section format for newly saved pages but not what to do when parsing a query page whose Sources section is absent, malformed, or references non-existent paths. Silently treating a missing Sources section as "no sources → never stale" could leave stale content undetected indefinitely. The spec should specify the fallback behavior.

---

### Q-33 (BLOCKING): FR-8.1 lint does not define behavior when query re-run returns zero relevant files — Task #33

**Section**: FR-8.1 step 3b

The `query` workflow can exit with code 3 ("No relevant files found"). In the context of lint, which must continue processing remaining stale pages, the spec does not define what to do when a query re-run returns no relevant results. The four plausible options (skip and leave stale, clear banner, delete page, abort lint) each produce different vault states. This must be specified.

---

### Q-34 (BLOCKING): FR-8.2 deduplication — "most recently saved" page has no reliable determination mechanism — Task #34

**Section**: FR-8.2 steps 4b, 4c, 4d

Deduplication uses the "most recently saved page" as the canonical survivor, but saved query pages have no creation timestamp in their content or in `index.md`. Filesystem mtimes are unreliable. The spec must nominate an authoritative recency signal — either row order in `index.md` (later = more recent) or `log.md` query-saved timestamps — and state this explicitly.

---

### Q-35 (BLOCKING): FR-8.3 deep health-check — LLM cannot detect contradictions or concept gaps from index.md titles alone — Task #35

**Section**: FR-8.3

FR-8.3 instructs sending only `index.md` (filenames + one-line descriptions) to the LLM for health-check. The LLM cannot meaningfully detect "claims that contradict claims in another page" or "important concepts mentioned across multiple pages" from one-line descriptions alone — it needs page content. The spec must either: (a) also send summary content (and specify a context budget for Part 3), (b) remove/down-scope Contradictions and Concept Gaps to what is detectable from titles, or (c) explicitly document this as a best-effort limitation with degraded output quality accepted.

---

### Q-36: FR-9.2 stale_warning field format not specified — Task #36

**Section**: FR-9.2

The `stale_warning` field is described as "a human-readable string" but no format example is given for multiple stale pages. The CLI uses a comma-separated list; the MCP field format should be explicitly specified (with an example) so agent callers can reliably parse or display it.

---

### Q-37: FR-10.2 behavior for unrecognized help argument not defined — Task #37

**Section**: FR-10.2

`codewiki help <command>` is defined for `ingest`, `analysis`, `query`, `lint`. What happens for `codewiki help foo`, `codewiki help mcp mcp`, or `codewiki help help`? The spec should define the error response and exit code for unrecognized arguments.

---

### Q-38: Acceptance test numbering is non-sequential in document order — Task #38

**Section**: Acceptance Tests

Tests are numbered #1–#19 but appear out of sequence: #19 (lint deduplication) is positioned physically between #14 and #15 in the document body. Subsequent tests proceed #15, #16, #17, #18. This makes test references ambiguous. The spec should either renumber tests sequentially in document order or explicitly state that numbers are identifiers only, not document position.

---

### Q-39 (BLOCKING): FR-3.8 change set — deleted summaries must be tracked before deletion, not after — Task #39

**Section**: FR-3.8 step 1, FR-3.7

The data flow runs FR-3.7 (deletions) then FR-3.8 (staleness detection). If the change set is built by scanning the vault after FR-3.7 runs, deleted summary paths will be missing from the change set — query pages referencing deleted summaries will not be flagged stale. The spec must explicitly state that the change set is built from an in-memory record of operations during the run, and that deleted summary paths are included even though they no longer exist on disk by the time FR-3.8 executes.

---

### Q-40: FR-8.1 lint internal query re-run — does it also write a log.md query entry? — Task #40

**Section**: FR-8.1 step 3b, FR-5

FR-5 says the `query` command SHALL append a `log.md` query entry. FR-8.1 says lint re-runs "the full `query` workflow." If lint goes through the shared core code path, it will write both a query entry and a `lint-resolved` entry per page, producing confusing log output. The spec must explicitly state whether the internal log.md query entry is suppressed during lint-triggered re-runs, and if so, how the shared code supports this without code duplication.

---

### Q-41: FR-5: how is the index.md one-line description for a saved query page generated? — Task #41

**Section**: FR-5 step d, FR-9.2

The Description column entry for a saved query page "SHALL contain a one-line summary of what the answer covers." No generation mechanism is specified — it could be the first sentence of the answer, a fixed template ("Answer to: [question]"), or an LLM-generated summary. The same gap applies to the MCP auto-save path. The mechanism must be specified because the test in acceptance test #13(c) requires a verifiable `index.md` description, and the lint deduplication (FR-8.2 step 3) uses descriptions from `index.md` to detect near-duplicate intent.

---

## Additional Observations (No New Question Tasks Needed)

1. **v1.5.0 version history is now misleading**: The version history entry for v1.5.0 describes adding "four MCP tools (`wiki_ingest`, `wiki_query`, `wiki_analysis`, `wiki_lint`)" — but v1.6.0 removed all tools except `wiki_query`. The current spec correctly reflects only `wiki_query`, but the v1.5.0 history entry is inaccurate. This is cosmetic (history is not normative) but could confuse readers tracing design evolution.

2. **FR-3.6 index.md scope for lint-report.md and log.md**: The spec says `index.md` covers "all current wiki pages: both summary files and saved query pages." Files like `lint-report.md`, `log.md`, and `overview.md` are implicitly excluded (they are neither summary files nor query pages). This is inferrable but could be stated explicitly for clarity.

3. **FR-8.2 step 2: skip threshold of "fewer than 2" query pages**: The deduplication step skips if fewer than 2 query pages exist. This is correct (deduplication requires at least 2 candidates). No question needed, but worth noting for completeness.

4. **FR-9 version history inconsistency with current spec (MCP tools count)**: See observation #1 above. Not a behavioral issue.

---

## Verdict

v1.7.0 adds substantial functionality (staleness detection, lint, MCP, help) that is largely well-specified. The rename in v1.7.0 itself is clean and introduces no issues. However, 5 of the 12 new questions are BLOCKING for implementation planning: the stale banner placement conflict (Q-30), banner detection mechanism (Q-31), zero-results lint behavior (Q-33), deduplication recency determination (Q-34), and change set timing for deletions (Q-39). The FR-8.3 logical impossibility (Q-35) is the most substantively broken requirement — it asks the LLM to do something the spec's own constraints make impossible.

**Minimum required before implementation planning**: Resolve Q-30, Q-31, Q-33, Q-34, Q-35, Q-39.
