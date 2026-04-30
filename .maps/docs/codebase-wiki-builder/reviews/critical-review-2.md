# Critical Review #2: Codebase Wiki Builder Specification (v1.1.0)

**Reviewer**: Critic Agent (MAPS)
**Review Date**: 2026-04-27
**Spec Version**: 1.1.0
**Task ID**: 17

---

## Summary

The revised specification (v1.1.0) successfully incorporates all 10 open questions raised in Critical Review #1. The resolutions are correctly and precisely written into the spec — none are superficially marked resolved without substantive changes. The spec is of high quality and is structurally complete.

However, the revisions introduced **3 new open questions** that must be resolved before implementation planning can proceed without ambiguity. Two of these are related to the new tiktoken-based context management additions (Q-7 and Q-10 resolutions), which introduced the concept of a "context window limit" without ever specifying what that limit is or where it comes from.

---

## Prior Question Verification

All 10 questions from Critical Review #1 are verified as properly incorporated:

| Question | Subject | Status | Evidence |
|----------|---------|--------|----------|
| Q-6 | Summary file naming `<name>.<ext>.md` | PASS | FR-3.5: explicit naming rule with example (`user_service.py` → `user_service.py.md`) |
| Q-7 | Analysis batching via tiktoken + directory tree + final synthesis | PASS | FR-4: full batch strategy with tiktoken pre-flight, directory-tree subdivision logic, and final synthesis step specified |
| Q-8 | Obsidian CLI targets active vault, no vault name config | PASS | FR-7: "invoking the Obsidian CLI against the active vault (the directory the tool is run from). No explicit vault name configuration is required." |
| Q-9 | LLM returns structured explicit+dynamic references; dynamic annotated `(inferred)` | PASS | FR-3.5: prompt instructs two-category list; dynamic references that resolve to real files annotated `(inferred)` |
| Q-10 | Query uses tiktoken; zero-results exits code 3 + "No relevant files found" message | PASS | FR-5: tiktoken fill to context window; empty JSON array → print `"No relevant files found for that query."` and exit 3 |
| Q-11 | Performance NFR removed | PASS | NFR section contains only Reliability and Security — no unmeasurable performance language |
| Q-12 | Exit code table added (0/1/2/3) | PASS | NFR "Exit Codes" subsection with complete table |
| Q-13 | Config validation hard-errors with informative messages | PASS | FR-2: exits code 1, prints config file path + offending field name + expected format |
| Q-14 | Acceptance test #2 uses MD5 footer comparison, not timestamps | PASS | AT-2: verifies MD5 in summary footer matches current source file MD5 |
| Q-15 | Empty vault exits code 1 with "run ingest" guidance | PASS | FR-4 and FR-5 both: check for `index.md`, exit 1 with `"The vault has no summaries. Run 'wiki ingest' first."` |

---

## New Open Questions Found: 3

### NQ-1 (BLOCKING): Context window size never specified for tiktoken budgeting — Task #19

**Section**: FR-4 (analysis), FR-5 (query)

FR-4 says batches must "fit within the context window limit" and FR-5 says to "fill up to the LLM context window limit." Both use tiktoken for token counting. However, the actual numeric limit is never defined anywhere in the spec. The config schema (FR-2) lists only five fields: codebase path, LLM provider, LLM model, `file_size_threshold`, and `inter_request_delay`. There is no `context_window_tokens` field.

Without a numeric budget, tiktoken counting cannot function — implementers must hardcode or invent a value. This is a BLOCKING gap because both the analysis batching algorithm and the query retrieval loop depend on it.

**Options discussed in question task**:
- (a) Hard-coded per-model lookup table (e.g., claude-sonnet-4-6 = 200,000 tokens)
- (b) Add `context_window_tokens` to `.wiki-config.json` with a sensible default
- (c) Single conservative default for all models (e.g., 100,000 tokens)

---

### NQ-2: Query answer citation format unspecified — Task #20

**Section**: FR-5, Acceptance Test #7

FR-5 says the answer "SHALL include citations indicating which summary files were consulted" but does not specify the citation format (inline, footnote, trailing "Sources" section, etc.). Without a defined format:
- The LLM prompt for answer generation cannot instruct the model on citation style
- Acceptance test #7 ("a non-empty answer is printed") cannot verify that citations are present or correctly formatted

This is not BLOCKING for implementation but will cause inconsistent output across runs and makes the acceptance test untestable.

---

### NQ-3: Query command — single summary exceeding context window budget, no handling defined — Task #21

**Section**: FR-5

FR-5 specifies filling summaries up to the context window limit in priority order. It does not specify what to do if a single top-ranked summary by itself exceeds the entire remaining token budget. A related edge case: if the context budget is consumed by the highest-priority summaries, lower-priority (but still LLM-identified as relevant) summaries are silently dropped. The spec does not indicate whether the answer should acknowledge dropped sources.

---

## Additional Observations (No New Question Tasks Needed)

1. **Backlink wikilink path format** — FR-3.5 specifies `[[relative/path/to/file]]` for backlinks and the Technical Context section clarifies "SHALL omit the `.md` extension." Since summary files are named `<source>.py.md`, the wikilink is `[[src/auth/login.py]]` — which is also the source file's vault-mirrored path minus `.md`. This is internally consistent. No question needed, but implementers should be aware this means wikilinks point at source-path mirrors, not separate summary identifiers.

2. **index.md table format** — FR-3.6 now mandates a markdown TABLE with exactly two columns. Acceptance test #1 checks "index.md lists all 10" but does not verify table format. The acceptance test is weaker than the requirement, but this is a test quality issue for the test writer to address rather than a spec gap.

3. **Analysis partial failure exit code** — The exit code table defines code 2 only for `ingest` partial success. If `analysis` encounters file read failures mid-run, the applicable exit code is ambiguous (0? 1? 2?). This is a minor edge case and may be adequately covered by the general code 1 definition, but noting it for awareness.

4. **`analysis` with zero summaries but existing index.md** — If `ingest` ran on a codebase with zero eligible files, it would write an empty `index.md`. The `analysis` command checks for `index.md` existence (not content), so it would proceed with an empty prompt. Very edge-case for a real codebase, but could cause a confusing LLM response. Acceptable for MVP.

---

## Checklist Results (v1.1.0)

### Completeness

| Criterion | Status | Notes |
|-----------|--------|-------|
| Clear problem statement | PASS | Unchanged and clear |
| User story / stakeholder context | PASS | Unchanged and clear |
| Measurable acceptance criteria | PASS | AT-2 now uses MD5 comparison |
| Functional requirements by capability | PASS | FR-1 through FR-7 well organized |
| Non-functional requirements | PASS | Performance NFR removed; Reliability and Security remain; Exit Codes added |
| Explicit constraints (DO NOT) | PASS | 12 constraints unchanged |
| Technical context | PASS | tiktoken added to stack; vault layout updated for `<name>.<ext>.md` |
| Out of scope section | PASS | Unchanged |
| Dependencies identified | PASS | tiktoken added to external deps table |

### Clarity

| Criterion | Status | Notes |
|-----------|--------|-------|
| Implementable without clarification | PARTIAL | Context window limit is undefined (NQ-1 BLOCKING); citation format ambiguous (NQ-2) |
| Active, specific language (SHALL/MUST) | PASS | Consistent use throughout |
| No ambiguous terms | PARTIAL | "context window limit" used multiple times without a value |
| Edge cases and error scenarios covered | PARTIAL | Oversized summary in query unhandled (NQ-3) |
| Given/When/Then format for workflows | PASS | 5 scenarios present |

### Specification Guidelines Compliance

| Criterion | Status | Notes |
|-----------|--------|-------|
| Specifies WHAT not HOW | PASS | No prescriptive implementation details |
| Includes "why" for non-obvious requirements | PASS | Rationale present for key decisions |
| References existing code patterns | N/A | Greenfield |
| Under 10K tokens | PASS | Approximately 4,500 words |

---

## Verdict

v1.1.0 is a high-quality revision that cleanly resolves all 10 prior questions. One new BLOCKING gap was introduced: the tiktoken-based context window management requires a numeric budget that is never defined in the spec. This must be resolved before implementation planning can specify the analysis batching and query retrieval algorithms. Two additional non-blocking gaps (citation format, oversized summary handling) improve robustness if resolved.

**Minimum required before implementation planning**: Resolve NQ-1 (context window size definition).
