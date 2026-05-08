# LLM Security Review: Query Cache Specification (Iteration 2)

## Review Metadata
- **Reviewer**: LLM Security Auditor Agent (MAPS)
- **Reviewed Document**: `.maps/docs/query-cache-have-i-answered-this-before-check/specification/spec.md` (v1.4.0)
- **Date**: 2026-05-08
- **Task ID**: 142
- **Prior Review**: Task #134 (spec v1.3.0) — 6 issues found (SEC-1 through SEC-6)
- **Status**: CLEAN — no new question tasks created

---

## Scope

This review is narrowly scoped to:
1. Verifying that the SEC-3 path traversal fix introduced in v1.4.0 is correctly and completely specified.
2. Checking whether the SEC-3 fix itself introduces any new security concerns.

SEC-1, SEC-2, SEC-4, SEC-5, SEC-6 were all deferred by the user (single-user, local-IO application) and are explicitly out of scope for this review.

---

## SEC-3 Fix: Verification

### What changed
FR-QC-2 Stage 2 now includes a mandatory 3-part path validation step (new step 5, items i–iii) that runs immediately after the LLM returns a path and before any file is opened on disk. Three acceptance tests (AT-15, AT-16, AT-17) were added, one per validation check.

### Check-by-check assessment

**Check i — Prefix check** (`path.startswith("queries/")`):
Correctly specified as a string prefix test. This is the fastest gate and eliminates the most obvious traversal attempts. The spec is explicit that the check is on the raw returned path string.
Verdict: **Correctly specified.**

**Check ii — Containment check** (`Path(vault_root / path).resolve().is_relative_to(vault_root / "queries/")`):
Correctly specified. This check handles all bypasses that the prefix check cannot catch: `..` components after the prefix (e.g., `queries/../../etc/passwd`), symlinks, and any OS-level path normalization edge cases. Uses `Path.is_relative_to()`, which is available in Python 3.9+ and the spec requires 3.12+ — no compatibility issue.
Verdict: **Correctly specified.**

**Check iii — Allowlist check** (path without `.md` must be a member of the pre-computed wikilink target set from `index.md`):
This is the strongest defense: even a path that passes both (i) and (ii) is rejected unless it was already in the `index.md` entry set used to build the Stage 2 prompt. The allowlist is pre-computed in Stage 2 step 1 before the LLM call, so the LLM cannot inject new entries. This check makes the validation essentially complete.
Verdict: **Correctly specified.**

**Ordering of checks:**
The checks are performed in sequence (i → ii → iii). Crucially, no file is opened until all three checks pass. The spec explicitly states "validate the path before opening any file on disk." The defensive ordering (cheap string check first, expensive resolve second, set-lookup third) is also efficient.
Verdict: **Correct.**

**Failure handling:**
All three validation failures are specified as silent cache misses ("treat the result as a cache miss immediately — do not raise an error"). This is consistent with FR-QC-9's defensive error handling philosophy and ensures no exception can propagate to the caller.
Verdict: **Correctly specified.**

### Acceptance test coverage

| Test | Check exercised | What it verifies |
|------|----------------|-----------------|
| AT-15 | Prefix check (i) | Path not beginning with `queries/` is rejected before any file open |
| AT-16 | Containment check (ii) | Path beginning with `queries/` but resolving outside `vault_root/queries/` is rejected |
| AT-17 | Allowlist check (iii) | Syntactically valid, safely-resolved path not in `index.md` set is rejected |

Each test requires verifying: (a) no file opened on disk, (b) cache miss result, (c) full pipeline runs, (d) no exception propagates. Coverage is correct and complete for the three specified checks.

---

## Does the SEC-3 Fix Introduce New Security Concerns?

### Finding: Minor `.md`-suffix ambiguity in allowlist check (not a security hole)

**Observation**: The spec states the allowlist check strips any `.md` suffix before comparing against the wikilink target set. The LLM prompt instructs the LLM to return wikilink paths in the no-extension Obsidian convention (e.g., `queries/how-does-auth-work`). However, an LLM hallucination could return a path with `.md` already appended (e.g., `queries/how-does-auth-work.md`). Stripping `.md` from this would yield `queries/how-does-auth-work`, which IS a valid allowlist member. The implementation would then attempt to open the file at `vault_root/queries/how-does-auth-work.md.md` (appending `.md` per step 5a). That file does not exist, so `read_query_page()` raises an exception, which is caught defensively (FR-QC-9) and treated as a cache miss.

**Security impact**: None. Worst case is a spurious cache miss — the full pipeline runs. No file outside the allowed set is accessed.

**Severity**: Informational only. No question task warranted.

### TOCTOU between allowlist construction and file open

**Observation**: The allowlist is built from `index.md` at the start of Stage 2. Between allowlist construction and the file open, `index.md` could be modified (e.g., entry removed). This would mean a path passes the allowlist check but the file no longer exists (or a new file was created at that path).

**Security impact**: None for a single-user local application. File-not-found raises an exception caught by FR-QC-9 (cache miss). A file newly created at a valid path slot would itself be a valid query page. This is already covered by the user's deferred rationale for SEC-1/2/4/5/6.

**Severity**: Not applicable given single-user, local-IO context.

### No new attack surface introduced

The 3-part validation consumes only the LLM-returned path string and the pre-computed `index.md` wikilink set. Both inputs were already present in the Stage 2 flow. The validation adds no new data sources, no new LLM calls, and no new filesystem reads before the checks pass. The fix is purely additive.

---

## Summary

The SEC-3 fix is **correctly and completely specified**. All three path validation checks are present in the right order, applied before any file open, and failures are silent cache misses. The three acceptance tests (AT-15, AT-16, AT-17) exercise each check independently with correct verification criteria. No new security issues are introduced by the fix.

**No new question tasks created.**

---

## Recommendation

The specification is ready for implementation. The only remaining note is the minor `.md`-suffix ambiguity documented above — this is self-correcting (fails to a cache miss) and does not require a spec change, but an implementer should be aware of it and handle the double-extension edge case gracefully in code (e.g., by normalizing the LLM response to strip any `.md` suffix before the allowlist check).
