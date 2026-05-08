# LLM Security Review: Query Cache Specification

## Review Metadata
- **Reviewer**: LLM Security Auditor Agent (MAPS)
- **Reviewed Document**: `.maps/docs/query-cache-have-i-answered-this-before-check/specification/spec.md` (v1.3.0)
- **Date**: 2026-05-08
- **Task ID**: 134
- **Status**: Issues found — 6 question tasks created

---

## Scope

This review audits the Stage 2 LLM pre-check introduced by the Query Cache feature (`query_cache.py`, `FR-QC-2`). The feature adds one new LLM call that receives:
1. The raw incoming user question (untrusted)
2. H1 titles read from query page files on disk (potentially untrusted)
3. One-line summaries from `index.md` (potentially untrusted)

The review also covers risks from verbatim cached answer content being returned to callers.

---

## Summary

**6 issues found.** The specification introduces a meaningful LLM integration attack surface concentrated in the Stage 2 prompt construction and the path-handling of the LLM's output. The most critical issues are path traversal (SEC-3) and prompt injection via both the incoming question (SEC-1) and disk-sourced H1 titles (SEC-2). The remaining issues are moderate-severity concerns about stored content injection and economic denial-of-service.

None of the issues are blockers if mitigated properly. The recommended fixes are all additive (new validation steps) and do not require redesigning the feature.

---

## Issues Found

### SEC-1: Prompt injection via incoming question in Stage 2 prompt
- **Severity**: High
- **Task**: #135
- **Location**: FR-QC-2, Stage 2, step 3 — Stage 2 prompt construction
- **Description**: The raw incoming user question is interpolated directly into the Stage 2 LLM prompt with no delimiter-based isolation or untrusted-input labeling. An adversary can craft a question containing instructions that override the prompt's matching logic, forcing a false-positive cache hit and returning a wrong cached answer to the caller.
- **Existing defense**: The spec uses f-string concatenation (preventing Python format-string injection) but this does not address LLM prompt injection.
- **Recommended fix**: Wrap the incoming question in XML-style delimiters (e.g., `<incoming-question>`) with an explicit instruction that the delimited text is untrusted user input. Also add a post-LLM validation step that rejects any returned path not present in the pre-computed `index.md` path set (allowlist).

---

### SEC-2: Prompt injection via H1 titles read from disk files
- **Severity**: Medium-High
- **Task**: #136
- **Location**: FR-QC-2, Stage 2, step 3 — "The question title is the actual H1 title read from each `queries/*.md` file on disk"
- **Description**: H1 titles from saved query page files are inserted verbatim into the Stage 2 prompt. A tampered file (or a file saved after a prior prompt injection attack) with a crafted H1 title can permanently poison the Stage 2 prompt for all future queries until the file is removed.
- **Recommended fix**: Wrap all disk-sourced H1 titles in delimiters (e.g., `<stored-question>`) in the Stage 2 prompt, and optionally enforce a maximum title length (e.g., 500 chars) with truncation.

---

### SEC-3: Path traversal via LLM-returned path in Stage 2
- **Severity**: High
- **Task**: #137
- **Location**: FR-QC-2, Stage 2, step 5a — "Locate the file at `<vault>/<returned-path>.md`"
- **Description**: The Stage 2 LLM returns a wikilink path that is used directly to construct a filesystem path without validation. If prompt injection (SEC-1 or SEC-2) causes the LLM to return a crafted path (e.g., `../overview`, `../../.env`), the implementation will attempt to open files outside the `queries/` directory. A path like `../log` would return vault log content verbatim as a "cached answer."
- **Recommended fix**: Mandate a three-part validation step before opening any LLM-returned path: (1) prefix check (`queries/`), (2) resolved-path containment check (`is_relative_to(vault_root / "queries/")`), (3) allowlist membership check (path must be in the set parsed from `index.md`). Treat any validation failure as a cache miss.

---

### SEC-4: Verbatim cached answer content returned to MCP callers without sanitization
- **Severity**: Medium
- **Task**: #138
- **Location**: FR-QC-4 — "answer SHALL be the full raw content read verbatim from the saved page file"
- **Description**: Cache hits return verbatim on-disk file content to MCP callers (AI coding agents) in the JSON response `answer` field. A poisoned query page — created through a prior prompt injection attack or direct filesystem write — would have its malicious content returned to every future caller who triggers a cache hit for that question, with no LLM re-evaluation. For MCP agents, this is an injected-instructions vector.
- **Recommended fix**: The spec should either (a) explicitly document the trusted-vault assumption and accept this risk, (b) strip control characters (null bytes, non-printable characters) from the cached answer before returning it, or (c) add a warning field to the MCP response indicating the answer is verbatim cached content.

---

### SEC-5: Adversarial false-negative bypass to inflate LLM API cost
- **Severity**: Low-Medium
- **Task**: #139
- **Location**: OQ-2 resolution — conservative Stage 2 prompt bias toward false negatives
- **Description**: The deliberate false-negative bias (correct for correctness) means an adversary can reliably bypass the cache by rephrasing questions slightly, causing every query to incur 3 LLM calls (pre-check + full pipeline) instead of 2 (full pipeline) or 0 (cache hit). In high-volume MCP deployments this is a 50% cost inflation attack requiring no system compromise — only question rephrasing.
- **Recommended fix**: Acknowledge this as an accepted v1 risk in the spec's Risks section, note that rate limiting or cost-budget mechanisms are out of scope for v1, and document the 3-call worst-case cost explicitly.

---

### SEC-6: one_line_summary from index.md passed unsanitized into cache hit QueryResult
- **Severity**: Low-Medium
- **Task**: #140
- **Location**: FR-QC-4 — "one_line_summary SHALL be the one-line description from the matching index.md row"
- **Description**: The `one_line_summary` returned on cache hits comes from the `index.md` description column, read verbatim from disk. A tampered `index.md` row could inject adversarial content into this field, which is included in the MCP JSON response. The spec specifies stripping the `⚠ stale` suffix but provides no broader sanitization guidance. It is also unclear whether the pipe-escaping applied at write time (`|` → `\|`) is reversed at read time.
- **Recommended fix**: Specify sanitization rules for descriptions read from `index.md` (strip leading/trailing whitespace, remove newlines and null bytes, enforce a 500-character maximum). Alternatively, document the trusted-vault assumption as covering this field.

---

## Non-Issues Noted

The following patterns were reviewed and found to be adequately handled:

- **f-string prompt construction**: `_build_relevance_prompt()` and `_build_answer_prompt()` in `query_engine.py` use f-string concatenation, which correctly prevents Python format-string injection (curly-brace expansion). The Stage 2 prompt should follow the same pattern. This is a partial mitigation for SEC-1 (addresses Python-level injection but not LLM prompt injection).
- **Staleness check as backstop**: The mandatory `has_stale_banner()` check before returning any cache hit provides a partial backstop against returning stale (but not adversarially modified) content.
- **LLMError fallthrough**: Stage 2 LLM errors correctly fall through to a cache miss (FR-QC-9), preventing LLM API failures from blocking queries.
- **Empty-slug skip**: Empty slugs correctly skip Stage 1 and proceed to Stage 2 rather than using a fallback slug, avoiding potential collision attacks.
- **Stage 2 skipped when no query pages exist**: The early-exit when `index.md` has no query rows correctly avoids the LLM call and eliminates the injection surface entirely for fresh vaults.

---

## Recommended Spec Changes (Summary)

| Issue | Required Spec Change |
|-------|----------------------|
| SEC-1 | Mandate delimiter-wrapped incoming question in Stage 2 prompt + post-LLM path allowlist validation |
| SEC-2 | Mandate delimiter-wrapped H1 titles in Stage 2 prompt + optional length cap |
| SEC-3 | Mandate 3-part path validation (prefix + containment + allowlist) before opening LLM-returned path |
| SEC-4 | Document trusted-vault assumption explicitly OR specify control-character stripping for cached answer |
| SEC-5 | Document 3-call worst-case cost as known limitation in Risks section |
| SEC-6 | Specify description sanitization rules OR document trusted-vault assumption as covering this field |

---

## Conclusion

The Query Cache feature is well-designed for its functional goals. The LLM security concerns are concentrated in the Stage 2 prompt construction and path handling — both of which can be addressed with additive validation steps that do not require architectural changes. SEC-3 (path traversal) and SEC-1 (question prompt injection) are the highest priority items and should be resolved before implementation begins.
