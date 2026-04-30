# LLM Security Review 2 — Implementation Plans

**Date**: 2026-04-30
**Reviewer**: LLM Security Auditor (automated, Task 86)
**Scope**: All 18 implementation plan files under `.maps/docs/codebase-wiki-builder/plans/`
**Prior review**: `.maps/docs/codebase-wiki-builder/reviews/llm-security-review-1.md` (spec-level; 5 issues, all deferred as "local single-user application; prompt injection security is not a concern")

---

## Methodology

This review examined all 18 implementation plans for LLM-specific security vulnerabilities that are:
1. NEW — not already raised and decided in review-1
2. IMPLEMENTATION-LEVEL — present only in the implementation detail (not detectable from the spec alone)
3. LLM-SECURITY-RELEVANT — affecting the correctness or integrity of how LLM prompts are constructed or processed

The 5 deferred issues from review-1 (all concerning LLM-layer prompt injection via user input, codebase content, or vault content) are explicitly NOT re-raised here. This review operates under the same assumption: local single-user application.

---

## Plans Reviewed

| Plan | Title | LLM-relevant? |
|------|-------|---------------|
| 01 | Project Scaffold | No (dependencies only) |
| 02 | Config | No (no LLM calls) |
| 03 | LLM Client | Yes — reviewed; no new issues (retry logic, SDK usage correct) |
| 04 | Vault Utilities | No |
| 05 | File Scanner | No |
| 06 | Summarizer | **YES — new issue found** |
| 07 | Index Writer | No |
| 08 | Index / Staleness | No |
| 09 | Ingest CLI | No |
| 10 | Analysis | **YES — new issue found** |
| 11 | Query Engine | No new issues (uses f-strings; safe) |
| 12 | Query Persistence | No |
| 13 | Query CLI | No |
| 14 | Lint Staleness | No new LLM security issues |
| 15 | MCP Server | No new issues (explicit schema validation; correct) |
| 16 | Lint Dedup + Healthcheck | **YES — new issues found** |
| 17 | Lint CLI / Help | No (orchestration only) |
| 18 | Obsidian CLI | No (subprocess only; no LLM calls) |

---

## Findings

### ISSUE-2-1: Python `str.format()` with Untrusted Content — 5 Affected Sites

**Severity**: Medium (correctness/reliability; data corruption risk; crash risk)
**Classification**: Python string processing vulnerability (not LLM-layer prompt injection)
**Task created**: Task 87

#### Description

Multiple prompt construction functions use Python's `str.format()` to splice untrusted content (source code, vault summaries, LLM-generated text) into prompt templates. Python's `str.format()` processes `{...}` tokens in the **substituted values** — not just in the template. This means:

- If the injected content contains `{relative_path}` (a named parameter that exists), Python silently substitutes the actual `relative_path` value into the content portion of the string.
- If the injected content contains `{unknown_key}` (a name that does not exist as a keyword argument), Python raises `KeyError`, crashing the function.

This is a Python-layer bug that corrupts or crashes prompt construction **before the string reaches the LLM**. It is unrelated to the LLM-layer prompt injection concerns deferred in review-1.

#### Affected Sites

**Site 1 — `summarizer.py`, `_build_prompt()` (plan 06)**

```python
return PROMPT_TEMPLATE.format(
    relative_path=relative_path,
    file_content=file_content,
)
```

- `file_content` = raw bytes of arbitrary source files (Python, JS, Rust, Go, C, etc.)
- Risk: Python source files routinely contain `{variable}` syntax in f-strings, `str.format()` calls, and similar constructs. A file with the content `"{relative_path}"` on any line would cause Python to substitute the actual relative path into the middle of the file content section, producing a corrupted prompt. A file with `{undefined_key}` raises `KeyError`, crashing summarization for that file with no recoverable error path.
- This is the highest-risk site because arbitrary source code from any language is being processed.

**Site 2 — `analysis.py`, `_build_partial_overview_prompt()` (plan 10)**

```python
prompt = PARTIAL_OVERVIEW_PROMPT.format(
    vault_dir=vault_dir_label,
    combined_summaries=combined,
)
```

- `combined` = concatenated vault summary content (LLM-generated Markdown)
- Risk: LLM-generated summaries of source code files will frequently contain `{...}` notation (code block examples, template placeholder documentation, struct/object syntax). Any `{vault_dir}` occurrence in a summary would cause silent substitution.

**Site 3 — `lint_dedup.py`, `_run_detection_pass()` (plan 16)**

```python
prompt = DETECTION_PROMPT.format(page_list=page_list)
```

- `page_list` = lines of `"filename: description"` from `index.md`
- Risk: descriptions are user-written and LLM-generated; lower risk than sites 1-2 but same class of vulnerability.

**Site 4 — `lint_dedup.py`, `_run_merge_pass()` (plan 16)**

```python
prompt = MERGE_PROMPT.format(pages_content=pages_content)
```

- `pages_content` = full content of saved query pages (LLM-generated answers, likely containing code blocks with `{...}` syntax)
- Risk: high — LLM-generated answers to programming questions almost certainly contain format-string examples.

**Site 5 — `lint_healthcheck.py`, `_run_batch_health_check()` and `_synthesize_health_check()` (plan 16)**

```python
prompt = HEALTH_CHECK_BATCH_PROMPT.format(
    index_content=index_content,
    combined_summaries=combined,
)
prompt = HEALTH_CHECK_SYNTHESIS_PROMPT.format(batch_findings=combined_sections)
```

- Both inject LLM-generated vault content (summary files, health-check findings text).
- Risk: same class as site 2.

#### Recommended Fix

Replace all `str.format()` calls at these sites with **f-string factory functions**. The prompt template constants can be retained as documentation but must not be used as runtime `.format()` templates when the substituted values are untrusted.

**Pattern: before (vulnerable)**
```python
PROMPT_TEMPLATE = """\
...
{file_content}
...
"""

def _build_prompt(relative_path, file_content):
    return PROMPT_TEMPLATE.format(
        relative_path=relative_path,
        file_content=file_content,
    )
```

**Pattern: after (safe)**
```python
def _build_prompt(relative_path: str, file_content: str) -> str:
    return f"""\
...
## File: {relative_path}

## Contents
```
{file_content}
```
..."""
```

F-strings are evaluated immediately in the local scope; they do not re-process `{...}` tokens within the runtime values of `relative_path` or `file_content`. The curly-brace content of those variables is treated as opaque string data.

**Question raised**: Task 87 asks the developer to confirm the preferred fix approach (f-string factory functions vs. string concatenation) so developer plans can be updated before implementation.

---

## Issues NOT Raised (Deliberate Exclusions)

### Re: Prior review-1 deferred issues (all 5)
The 5 issues from review-1 are all LLM-layer prompt injection concerns (user question text, codebase content, vault content reaching the LLM as potential instructions). All were deferred with the rationale "local single-user application; prompt injection security is not a concern." This review confirms those 5 issues remain in the same state — they are present in the implementation plans (the same content channels are used) and remain deferred. They are not re-raised here.

### Re: `str.format()` vs. f-string in non-LLM contexts
Some other modules use `str.format()` for non-LLM strings (e.g., log entry formatting, report headers). Those are not in scope — the vulnerability only manifests when the formatted string is passed to `llm_client.complete()` and when the substituted values come from untrusted sources.

### Re: Query engine (plan 11)
`run_query()` constructs prompts using f-strings throughout (e.g., `f"Question: {question}\n\n"`, `f"Wiki Index:\n{index_content}"`). F-strings are safe for this purpose — no `.format()` vulnerability applies. No issue raised.

### Re: MCP server (plan 15)
`_handle_wiki_query()` delegates to `run_query()` after parameter validation. No new prompt construction at the MCP layer. No issue raised.

### Re: `lint_staleness.py` (plan 14)
Plan 14's `_run_internal_query()` uses `typer.Exit(code=3)` to signal the no-relevant-files case, but plan 11 specifies `run_query()` raises `NoRelevantFilesError`. This is an architectural inconsistency (not a security issue) that would cause the lint staleness resolution to malfunction. It is noted here for completeness but is outside the LLM security scope of this review — it should be raised separately if not already tracked.

---

## Summary

| Issue ID | Type | Severity | Status | Plans Affected |
|----------|------|----------|--------|----------------|
| ISSUE-2-1 | Python `str.format()` with untrusted content | Medium | Open (Task 87) | 06, 10, 16 |

One new implementation-level security issue was identified. It is a Python string processing vulnerability that affects 5 prompt construction sites across 3 modules. A question task (Task 87) has been created for developer decision on the fix approach.

No new LLM-layer prompt injection issues were found beyond those already deferred in review-1.
