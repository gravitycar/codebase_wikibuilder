# Implementation Plan: Implement query_cache.py — Core Cache Module

## Spec Context

This plan implements FR-QC-1 and FR-QC-2 from the query-cache specification. It creates the new
`codebase_wiki_builder/query_cache.py` module, which is the single entry point for all cache
lookup logic. The module exposes `check_query_cache()`, called from `run_query()` in
`query_engine.py` after `index.md` is read but before the first LLM call.

Catalog item: Implement query_cache.py — Core Cache Module
Specification sections: FR-QC-1, FR-QC-2, FR-QC-3, FR-QC-9
Acceptance criteria addressed: AT-1 through AT-17 (all cache hit/miss/SEC-3 scenarios)

---

## Dependencies

- **Blocked by**:
  - plan-01 (`has_stale_banner` renamed to public) — `staleness.has_stale_banner()` must be public
  - plan-02 (`parse_existing_index` renamed to public) — `index_writer.parse_existing_index()` must be public
  - plan-03 (QueryResult cache fields) — `QueryResult` must have `from_cache`, `cached_path`, `cached_at`
- **Does NOT block**: plan-05 is itself the cache module; integration plans (CLI, MCP, run_query) depend on this plan
- **Uses**:
  - `codebase_wiki_builder.vault.slugify` — slug derivation for Stage 1
  - `codebase_wiki_builder.query_persistence.read_query_page` — parse candidate pages
  - `codebase_wiki_builder.staleness.has_stale_banner` — staleness check (after plan-01)
  - `codebase_wiki_builder.index_writer.parse_existing_index` — build Stage 2 candidate set (after plan-02)
  - `codebase_wiki_builder.query_engine.QueryResult` — extended dataclass (after plan-03)
  - `codebase_wiki_builder.llm_client.LLMClient` — Stage 2 LLM call (TYPE_CHECKING only)
  - `codebase_wiki_builder.config.WikiConfig` — passed through to maintain consistent API (TYPE_CHECKING only)

---

## File Changes

### New Files

- `codebase_wiki_builder/query_cache.py` — new module; single public function `check_query_cache()`

### Modified Files

None — the integration into `run_query()` is covered by a separate integration plan.

---

## Implementation Details

### Module: `codebase_wiki_builder/query_cache.py`

**Purpose**: Implements the two-stage cache lookup. Returns a `QueryResult` with `from_cache=True`
on a cache hit, or `None` on a cache miss. Never raises to the caller.

**Public API**:

```python
def check_query_cache(
    question: str,
    vault_root: Path,
    index_content: str,
    llm_client: "LLMClient",
    config: "WikiConfig",
) -> "QueryResult | None":
```

#### File header and imports

```python
"""Query cache for Codebase Wiki Builder.

Implements check_query_cache(), a two-stage pre-check that detects when an
existing saved query page already answers the user's question:

  Stage 1 — Slug walk: filesystem-only; O(1) for typical vaults.
  Stage 2 — LLM pre-check: one LLM call against existing query page titles.

Returns a QueryResult with from_cache=True on a hit, or None on a miss.
Never raises exceptions to the caller.

Public API:
  - check_query_cache(): main entry point
"""

from __future__ import annotations

import logging
import re
import string
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codebase_wiki_builder.config import WikiConfig
    from codebase_wiki_builder.llm_client import LLMClient
    from codebase_wiki_builder.query_engine import QueryResult

from codebase_wiki_builder.index_writer import parse_existing_index
from codebase_wiki_builder.query_persistence import read_query_page
from codebase_wiki_builder.staleness import has_stale_banner
from codebase_wiki_builder.vault import slugify

logger = logging.getLogger(__name__)
```

Note: `QueryResult` is imported only under `TYPE_CHECKING` to avoid a circular import
(`query_engine` imports `query_cache` after integration, so importing `QueryResult` at module
level would create a cycle). At runtime, construct `QueryResult` with a deferred import inside
`check_query_cache()`:

```python
from codebase_wiki_builder.query_engine import QueryResult  # deferred, inside function body
```

#### Question normalization helper

```python
def _normalize_question(text: str) -> str:
    """Normalize a question for exact-match comparison.

    1. Lowercase
    2. Strip all punctuation characters
    3. Collapse whitespace runs to a single space
    4. Strip leading/trailing whitespace
    """
    lowered = text.lower()
    no_punct = lowered.translate(str.maketrans("", "", string.punctuation))
    collapsed = re.sub(r"\s+", " ", no_punct)
    return collapsed.strip()
```

This helper is used in Stage 1 to compare the incoming question against the stored H1 title.
Both sides are normalized identically before comparison.

#### Stage 1 — Slug walk

```python
def _stage1_slug_walk(
    question: str,
    vault_root: Path,
    stale_warnings: list[str],
    index_descriptions: dict[str, str],
) -> "QueryResult | None":
    """Walk queries/<slug>.md, <slug>-2.md, ... looking for an H1 match.

    Returns a QueryResult on a non-stale hit, or None on a miss.
    A stale hit also returns None immediately (no sibling fallback).
    """
    from codebase_wiki_builder.query_engine import QueryResult

    slug = slugify(question)
    if not slug:
        logger.debug("Stage 1: empty slug for question %r; skipping", question)
        return None

    queries_dir = vault_root / "queries"
    normalized_incoming = _normalize_question(question)

    # Walk slug.md, slug-2.md, slug-3.md, ... until no file exists
    candidate_path = queries_dir / f"{slug}.md"
    suffix = 2
    while candidate_path.exists():
        try:
            page = read_query_page(candidate_path)
        except Exception as exc:
            logger.debug("Stage 1: failed to parse %s: %s", candidate_path, exc)
            # Advance to next suffix and continue
            candidate_path = queries_dir / f"{slug}-{suffix}.md"
            suffix += 1
            continue

        # H1 comparison (case-insensitive, strip punctuation/whitespace)
        if _normalize_question(page.question) == normalized_incoming:
            # Staleness check — mandatory before returning any hit
            if has_stale_banner(page.raw_content):
                logger.debug(
                    "Stage 1: slug match on %s but page is stale; full miss",
                    candidate_path,
                )
                return None  # Stale hit = full miss, no sibling fallback

            # Non-stale match — build QueryResult
            vault_rel = candidate_path.relative_to(vault_root)
            wikilink_key = vault_rel.with_suffix("").as_posix()  # e.g. "queries/auth"
            one_line = _strip_stale_suffix(index_descriptions.get(wikilink_key, ""))
            cached_at = page.saved_at if page.saved_at else None

            logger.debug("Stage 1: cache hit on %s", candidate_path)
            return QueryResult(
                answer=page.raw_content,
                sources=page.sources,
                one_line_summary=one_line,
                stale_warnings=stale_warnings,
                from_cache=True,
                cached_path=Path(vault_rel.as_posix()),
                cached_at=cached_at,
            )

        # H1 mismatch — advance to next numeric suffix
        candidate_path = queries_dir / f"{slug}-{suffix}.md"
        suffix += 1

    logger.debug("Stage 1: no match found for slug %r", slug)
    return None
```

**Key invariant on `answer` field**: Per spec FR-QC-4, the `answer` field on a cache hit SHALL be
the full raw content of the saved file (`page.raw_content`), not just `answer_body`. This is
"verbatim raw file content — answer body plus ## Sources section — exactly as originally written."

#### Stage 2 — LLM pre-check

```python
def _stage2_llm_precheck(
    question: str,
    vault_root: Path,
    index_content: str,
    llm_client: "LLMClient",
    stale_warnings: list[str],
    index_descriptions: dict[str, str],
) -> "QueryResult | None":
    """Use one LLM call to find a semantically equivalent cached answer.

    Returns a QueryResult on a non-stale hit, or None on a miss or error.
    """
    from codebase_wiki_builder.query_engine import QueryResult

    # Step 1: Collect query page rows from the pre-parsed index_descriptions
    query_rows: list[tuple[str, str]] = []  # (wikilink_target_without_md, description)
    for wikilink_target, description in index_descriptions.items():
        if wikilink_target.startswith("queries/"):
            query_rows.append((wikilink_target, description))

    if not query_rows:
        logger.debug("Stage 2: no query pages in index; skipping LLM call")
        return None

    # Step 2: Build (real H1 title, description) pairs by reading each file
    # Pre-compute the allowlist set of valid wikilink targets (SEC-3 check iii)
    valid_targets: set[str] = set()
    candidates: list[tuple[str, str, str]] = []  # (wikilink_target, real_h1_title, description)

    for wikilink_target, description in query_rows:
        file_path = vault_root / (wikilink_target + ".md")
        try:
            page = read_query_page(file_path)
            valid_targets.add(wikilink_target)
            candidates.append((wikilink_target, page.question, description))
        except Exception as exc:
            logger.debug("Stage 2: failed to parse %s: %s", file_path, exc)
            # Skip this candidate — not a hard error

    if not candidates:
        logger.debug("Stage 2: no parseable candidates; skipping LLM call")
        return None

    # Step 3: Build conservative LLM prompt
    prompt = _build_stage2_prompt(question, candidates)

    # Step 4: Call LLM
    try:
        raw_response = llm_client.complete(prompt)
    except Exception as exc:
        logger.warning("Stage 2: LLM call failed (%s); treating as cache miss", exc)
        return None

    # Step 5: Parse the returned path (or NO_MATCH sentinel)
    returned_path = _parse_stage2_response(raw_response)
    if returned_path is None:
        logger.debug("Stage 2: LLM returned NO_MATCH")
        return None

    # Step 6: SEC-3 path validation — all three checks must pass
    if not _validate_stage2_path(returned_path, vault_root, valid_targets):
        logger.debug("Stage 2: SEC-3 validation failed for path %r; cache miss", returned_path)
        return None

    # Step 7: Open the validated file
    file_path = vault_root / (returned_path + ".md")
    try:
        page = read_query_page(file_path)
    except Exception as exc:
        logger.debug("Stage 2: failed to parse validated file %s: %s", file_path, exc)
        return None

    # Staleness check
    if has_stale_banner(page.raw_content):
        logger.debug(
            "Stage 2: match on %s but page is stale; full miss (no sibling fallback)",
            file_path,
        )
        return None

    # Step 8: Construct QueryResult
    vault_rel = file_path.relative_to(vault_root)
    one_line = _strip_stale_suffix(index_descriptions.get(returned_path, ""))
    cached_at = page.saved_at if page.saved_at else None

    logger.debug("Stage 2: cache hit on %s", file_path)
    return QueryResult(
        answer=page.raw_content,
        sources=page.sources,
        one_line_summary=one_line,
        stale_warnings=stale_warnings,
        from_cache=True,
        cached_path=Path(vault_rel.as_posix()),
        cached_at=cached_at,
    )
```

#### SEC-3 path validation helper

```python
def _validate_stage2_path(
    path: str,
    vault_root: Path,
    valid_targets: set[str],
) -> bool:
    """Apply the three SEC-3 checks to an LLM-returned path.

    The path is the wikilink target WITHOUT the .md extension (e.g. "queries/auth").

    Check i  — Prefix: path must start with "queries/"
    Check ii — Containment: resolved absolute path must be under vault_root/queries/
    Check iii — Allowlist: path must be in valid_targets (pre-computed from index.md)

    Returns True only when all three checks pass. Any failure returns False silently.
    """
    # Check i: prefix
    if not path.startswith("queries/"):
        logger.debug("SEC-3 prefix check failed: %r", path)
        return False

    # Check ii: containment (prevent path traversal after .md append)
    queries_dir = vault_root / "queries"
    try:
        resolved = (vault_root / (path + ".md")).resolve()
        if not resolved.is_relative_to(queries_dir.resolve()):
            logger.debug("SEC-3 containment check failed: %r resolves outside queries/", path)
            return False
    except Exception as exc:
        logger.debug("SEC-3 containment check error for %r: %s", path, exc)
        return False

    # Check iii: allowlist
    if path not in valid_targets:
        logger.debug("SEC-3 allowlist check failed: %r not in valid_targets", path)
        return False

    return True
```

#### Stage 2 prompt builder

```python
def _build_stage2_prompt(
    question: str,
    candidates: list[tuple[str, str, str]],
) -> str:
    """Build the conservative Stage 2 LLM pre-check prompt.

    candidates: list of (wikilink_target, real_h1_title, description) triples
    """
    entries = "\n".join(
        f"  Path: {target}\n  Question: {title}\n  Summary: {desc}"
        for target, title, desc in candidates
    )
    return (
        "You are a cache lookup assistant. Your task is to determine whether an existing "
        "saved answer already answers the incoming question.\n\n"
        "IMPORTANT: Be very conservative. Only declare a match when you are HIGHLY CONFIDENT "
        "that BOTH of the following conditions are met simultaneously:\n"
        "  (a) The stored question is a strong semantic match to the incoming question — "
        "they are asking about the same thing.\n"
        "  (b) The existing answer completely answers the incoming question — not just "
        "partially.\n\n"
        "If you are uncertain about either condition, respond with NO_MATCH. "
        "False negatives (missing a valid match) are preferred over false positives "
        "(returning an incomplete or wrong answer).\n\n"
        "If a match is found, respond with ONLY the path value (e.g. queries/auth-flow). "
        "Do not add any explanation, punctuation, or other text.\n"
        "If no match is found, respond with exactly: NO_MATCH\n\n"
        f"Incoming question: {question}\n\n"
        f"Existing saved answers:\n{entries}"
    )
```

#### Stage 2 response parser

```python
def _parse_stage2_response(raw: str) -> str | None:
    """Parse the LLM Stage 2 response.

    Returns the wikilink path string (e.g. "queries/auth-flow") if the LLM
    declared a match, or None if it returned NO_MATCH or an unrecognizable response.
    """
    cleaned = raw.strip()
    if not cleaned or cleaned.upper() == "NO_MATCH":
        return None
    # Accept the first non-empty word — LLM may add trailing punctuation/whitespace
    # Only accept if it starts with "queries/" to filter out garbled responses
    first_token = cleaned.split()[0].rstrip(".,;:")
    if first_token.startswith("queries/"):
        return first_token
    return None
```

#### Stale suffix stripper

```python
def _strip_stale_suffix(description: str) -> str:
    """Remove the ' ⚠ stale' annotation from an index.md description if present."""
    return description.replace(" ⚠ stale", "").strip()
```

#### Main public entry point

```python
def check_query_cache(
    question: str,
    vault_root: Path,
    index_content: str,
    llm_client: "LLMClient",
    config: "WikiConfig",
) -> "QueryResult | None":
    """Attempt to return a cached answer for question.

    Runs Stage 1 (slug walk) first. If Stage 1 misses, runs Stage 2 (LLM pre-check).
    Returns a QueryResult with from_cache=True on a hit, or None on a miss.
    All exceptions are caught internally — never raises to the caller.

    Args:
        question:      The incoming question string (raw, unmodified).
        vault_root:    Absolute path to the vault root.
        index_content: Contents of index.md (already read by run_query()).
        llm_client:    The same LLMClient used by run_query() (for Stage 2).
        config:        WikiConfig (passed through; currently unused by cache logic).

    Returns:
        QueryResult with from_cache=True on a cache hit, or None on a miss.
    """
    try:
        # Pre-compute index descriptions once — used by both stages
        index_descriptions = parse_existing_index(vault_root)

        # stale_warnings is derived from index_content by run_query() before calling us;
        # we reconstruct it here from index_descriptions so query_cache.py is self-contained.
        # (run_query() passes index_content rather than the already-computed list.)
        stale_warnings = _collect_stale_warnings_from_content(index_content)

        # Stage 1 — slug walk (no LLM)
        result = _stage1_slug_walk(
            question=question,
            vault_root=vault_root,
            stale_warnings=stale_warnings,
            index_descriptions=index_descriptions,
        )
        if result is not None:
            return result

        # Stage 2 — LLM pre-check
        result = _stage2_llm_precheck(
            question=question,
            vault_root=vault_root,
            index_content=index_content,
            llm_client=llm_client,
            stale_warnings=stale_warnings,
            index_descriptions=index_descriptions,
        )
        return result  # None on miss

    except Exception as exc:
        logger.warning("check_query_cache: unexpected error, treating as miss: %s", exc)
        return None
```

**Note on `stale_warnings`**: `run_query()` already computes `stale_warnings` before calling
`check_query_cache()`. However, `check_query_cache()` receives `index_content` (not the computed
list) to keep the signature clean. The module includes a small private helper that re-derives the
list from `index_content`:

```python
import re as _re
_STALE_ROW_RE = re.compile(r"\[\[([^\]]+)\]\].*⚠ stale")

def _collect_stale_warnings_from_content(index_content: str) -> list[str]:
    """Derive stale_warnings list from index_content (mirrors query_engine logic)."""
    warnings = []
    for line in index_content.splitlines():
        m = _STALE_ROW_RE.search(line)
        if m:
            warnings.append(m.group(1) + ".md")
    return warnings
```

---

## Error Handling

The error-handling policy follows FR-QC-9:

| Condition | Behavior |
|-----------|----------|
| `read_query_page()` raises any exception (Stage 1) | Log at DEBUG; skip that candidate; continue walk |
| `read_query_page()` raises any exception (Stage 2) | Log at DEBUG; skip that candidate; exclude from valid_targets |
| LLM call raises `LLMError` or any exception (Stage 2) | Log at WARNING; return `None` (cache miss) |
| SEC-3 validation failure | Log at DEBUG; return `None` (cache miss); no exception |
| Any unexpected exception in `check_query_cache()` | Log at WARNING; return `None` (cache miss) |
| `saved_at` is empty string (from `QueryPage`) | `cached_at = None` — not a miss |
| `one_line_summary` not found in index_descriptions | `one_line_summary = ""` — not a miss |

The outer `try/except Exception` in `check_query_cache()` is the last-resort safety net. Inner
helpers have their own targeted exception handling as shown above.

---

## Unit Test Specifications

**File**: `tests/test_query_cache.py` (new file)

### Test class structure

```
TestNormalizeQuestion
  test_lowercases
  test_strips_punctuation
  test_collapses_whitespace
  test_strips_leading_trailing

TestStripStaleSuffix
  test_strips_stale_annotation
  test_passthrough_when_no_annotation
  test_strips_with_extra_spaces

TestValidateStage2Path
  test_valid_path_passes
  test_prefix_check_rejects_non_queries_prefix
  test_prefix_check_rejects_traversal_with_queries_prefix
  test_containment_check_rejects_path_traversal
  test_allowlist_check_rejects_unlisted_path
  test_all_three_checks_required

TestParseStage2Response
  test_no_match_returns_none
  test_valid_path_returned
  test_empty_response_returns_none
  test_strips_trailing_punctuation
  test_non_queries_prefix_returns_none

TestStage1SlugWalk
  test_empty_slug_returns_none
  test_slug_hit_exact_match
  test_slug_hit_case_insensitive_normalized
  test_slug_hit_numeric_suffix
  test_stale_hit_returns_none_full_miss
  test_no_candidate_file_returns_none
  test_h1_mismatch_continues_walk
  test_parse_failure_skips_candidate_continues
  test_answer_is_raw_content

TestStage2LlmPrecheck
  test_no_query_rows_skips_llm
  test_no_parseable_candidates_skips_llm
  test_llm_returns_no_match
  test_llm_returns_valid_path_hit
  test_llm_error_returns_none
  test_stale_match_returns_none
  test_sec3_prefix_failure_returns_none
  test_sec3_containment_failure_returns_none
  test_sec3_allowlist_failure_returns_none

TestCheckQueryCache
  test_stage1_hit_skips_stage2
  test_stage1_miss_runs_stage2
  test_both_stages_miss_returns_none
  test_unexpected_exception_returns_none
  test_from_cache_true_on_hit
  test_cached_path_set_on_hit
  test_cached_at_set_on_hit
  test_cached_at_none_when_saved_at_missing
  test_stale_warnings_included_in_result
```

### Key test scenarios

#### SEC-3 prefix check (AT-15)

```python
def test_sec3_prefix_failure_returns_none(self, tmp_path):
    """LLM returns a path not starting with 'queries/'; no file is opened."""
    valid_targets = {"queries/auth"}
    for bad_path in ["../sensitive/file", "summaries/auth", "queries"]:
        result = _validate_stage2_path(bad_path, tmp_path, valid_targets)
        assert result is False
```

#### SEC-3 containment check (AT-16)

```python
def test_sec3_containment_failure_returns_none(self, tmp_path):
    """LLM returns 'queries/../../etc/passwd'; resolve() exits queries/."""
    valid_targets = {"queries/../../etc/passwd"}  # even if in allowlist
    result = _validate_stage2_path("queries/../../etc/passwd", tmp_path, valid_targets)
    assert result is False
```

#### SEC-3 allowlist check (AT-17)

```python
def test_sec3_allowlist_failure_returns_none(self, tmp_path):
    """LLM returns a syntactically valid path not in pre-computed valid_targets."""
    queries_dir = tmp_path / "queries"
    queries_dir.mkdir()
    # path begins with queries/ and resolves within queries/ but is not in the allowlist
    valid_targets = {"queries/auth"}  # "queries/other-page" is NOT in this set
    result = _validate_stage2_path("queries/other-page", tmp_path, valid_targets)
    assert result is False
```

#### Stale hit is a full miss — no sibling fallback (AT-4)

```python
def test_stale_hit_returns_none_full_miss(self, tmp_path):
    """Stage 1 finds a slug match with a stale banner — returns None, does not
    check slug-2.md even if it exists with the same H1."""
    queries_dir = tmp_path / "queries"
    queries_dir.mkdir()

    stale_content = (
        "# How does auth work?\n\n"
        "> [!warning] Stale Content\n"
        "> Source changed.\n\n"
        "The answer.\n\n## Sources\n- src/auth.py.md\n\n"
        "## Page Metadata\nsaved_at: 2026-01-01 00:00:00 UTC\nupdated_at: 2026-01-01 00:00:00 UTC\n"
    )
    fresh_content = (
        "# How does auth work?\n\nThe fresh answer.\n\n## Sources\n- src/auth.py.md\n\n"
        "## Page Metadata\nsaved_at: 2026-01-01 00:00:00 UTC\nupdated_at: 2026-01-01 00:00:00 UTC\n"
    )
    (queries_dir / "how-does-auth-work.md").write_text(stale_content, encoding="utf-8")
    (queries_dir / "how-does-auth-work-2.md").write_text(fresh_content, encoding="utf-8")

    # Build minimal index.md for index_descriptions
    (tmp_path / "index.md").write_text(
        "| File | Description |\n|------|-------------|\n"
        "| [[queries/how-does-auth-work]] | Explains auth ⚠ stale |\n"
        "| [[queries/how-does-auth-work-2]] | Explains auth |\n",
        encoding="utf-8",
    )
    index_descriptions = {"queries/how-does-auth-work": "Explains auth ⚠ stale",
                          "queries/how-does-auth-work-2": "Explains auth"}

    result = _stage1_slug_walk(
        question="How does auth work?",
        vault_root=tmp_path,
        stale_warnings=[],
        index_descriptions=index_descriptions,
    )
    # Stale primary = full miss. The -2 sibling is NOT checked.
    assert result is None
```

#### Stage 1 hit with numeric suffix (AT-3)

```python
def test_slug_hit_numeric_suffix(self, tmp_path):
    """Stage 1 finds a match on slug-2.md when slug.md has a different H1."""
    queries_dir = tmp_path / "queries"
    queries_dir.mkdir()

    other_content = (
        "# Some other question?\n\nOther answer.\n\n## Sources\n- src/other.py.md\n\n"
        "## Page Metadata\nsaved_at: 2026-01-01 00:00:00 UTC\nupdated_at: 2026-01-01 00:00:00 UTC\n"
    )
    target_content = (
        "# How does auth work?\n\nThe answer.\n\n## Sources\n- src/auth.py.md\n\n"
        "## Page Metadata\nsaved_at: 2026-04-29 10:00:00 UTC\nupdated_at: 2026-04-29 10:00:00 UTC\n"
    )
    (queries_dir / "how-does-auth-work.md").write_text(other_content, encoding="utf-8")
    (queries_dir / "how-does-auth-work-2.md").write_text(target_content, encoding="utf-8")
    (tmp_path / "index.md").write_text(
        "| File | Description |\n|------|-------------|\n"
        "| [[queries/how-does-auth-work]] | Other |\n"
        "| [[queries/how-does-auth-work-2]] | Explains auth |\n",
        encoding="utf-8",
    )
    index_descriptions = {"queries/how-does-auth-work": "Other",
                          "queries/how-does-auth-work-2": "Explains auth"}

    result = _stage1_slug_walk(
        question="How does auth work?",
        vault_root=tmp_path,
        stale_warnings=[],
        index_descriptions=index_descriptions,
    )
    assert result is not None
    assert result.from_cache is True
    assert result.cached_path == Path("queries/how-does-auth-work-2.md")
    assert result.cached_at == "2026-04-29 10:00:00 UTC"
```

#### `answer` field is verbatim raw content (spec FR-QC-4)

```python
def test_answer_is_raw_content(self, tmp_path):
    """The answer field on a cache hit is the full raw file content, not answer_body."""
    queries_dir = tmp_path / "queries"
    queries_dir.mkdir()

    raw_content = (
        "# How does auth work?\n\nThe answer text.\n\n## Sources\n- src/auth.py.md\n\n"
        "## Page Metadata\nsaved_at: 2026-04-29 10:00:00 UTC\nupdated_at: 2026-04-29 10:00:00 UTC\n"
    )
    (queries_dir / "how-does-auth-work.md").write_text(raw_content, encoding="utf-8")
    (tmp_path / "index.md").write_text(
        "| File | Description |\n|------|-------------|\n"
        "| [[queries/how-does-auth-work]] | Explains auth |\n",
        encoding="utf-8",
    )
    index_descriptions = {"queries/how-does-auth-work": "Explains auth"}

    result = _stage1_slug_walk(
        question="How does auth work?",
        vault_root=tmp_path,
        stale_warnings=[],
        index_descriptions=index_descriptions,
    )
    assert result is not None
    assert result.answer == raw_content
```

#### Stage 2 LLM error resilience (AT-12)

```python
def test_llm_error_returns_none(self, tmp_path, monkeypatch):
    """Stage 2 LLM call raises LLMError — result is None, no exception propagates."""
    from unittest.mock import MagicMock
    from codebase_wiki_builder.query_cache import _stage2_llm_precheck

    queries_dir = tmp_path / "queries"
    queries_dir.mkdir()
    page_content = (
        "# How does auth work?\n\nAnswer.\n\n## Sources\n- src/auth.py.md\n\n"
        "## Page Metadata\nsaved_at: 2026-01-01 00:00:00 UTC\nupdated_at: 2026-01-01 00:00:00 UTC\n"
    )
    (queries_dir / "how-does-auth-work.md").write_text(page_content, encoding="utf-8")

    mock_client = MagicMock()
    mock_client.complete.side_effect = Exception("LLM unavailable")

    index_descriptions = {"queries/how-does-auth-work": "Explains auth"}
    index_content = (
        "| File | Description |\n|------|-------------|\n"
        "| [[queries/how-does-auth-work]] | Explains auth |\n"
    )
    result = _stage2_llm_precheck(
        question="How does auth work?",
        vault_root=tmp_path,
        index_content=index_content,
        llm_client=mock_client,
        stale_warnings=[],
        index_descriptions=index_descriptions,
    )
    assert result is None
```

#### `check_query_cache` — unexpected exception returns None (AT-11)

```python
def test_unexpected_exception_returns_none(self, tmp_path, monkeypatch):
    """Any unexpected exception in check_query_cache is swallowed; returns None."""
    from unittest.mock import patch
    from codebase_wiki_builder.query_cache import check_query_cache

    with patch(
        "codebase_wiki_builder.query_cache.parse_existing_index",
        side_effect=RuntimeError("Unexpected"),
    ):
        mock_client = MagicMock()
        result = check_query_cache(
            question="Q?",
            vault_root=tmp_path,
            index_content="",
            llm_client=mock_client,
            config=MagicMock(),
        )
    assert result is None
```

---

## Notes

1. **Circular import avoidance**: `query_engine.py` will import `query_cache.py` (after the
   integration plan). Therefore `query_cache.py` must NOT import `QueryResult` at module level.
   Use `from codebase_wiki_builder.query_engine import QueryResult` inside function bodies. The
   `TYPE_CHECKING` guard covers the type annotation only.

2. **`parse_existing_index` is called with `vault_root`**: The function reads `index.md` from
   `vault_root / "index.md"`. Although `check_query_cache()` also receives `index_content` (the
   already-read string), `parse_existing_index()` takes `vault_root` and reads the file itself.
   These two reads will agree in normal operation. If a future optimization is needed, a
   `parse_existing_index_from_content(content)` variant could be added — but that is out of scope.

3. **`valid_targets` construction**: The set is built from successful `read_query_page()` calls in
   Stage 2 step 2. Files that fail to parse are excluded from `valid_targets`. This means the SEC-3
   allowlist check will reject any path whose file is unreadable — a conservative and correct
   behavior.

4. **No `config` parameter use**: `WikiConfig` is included in the signature per spec FR-QC-1.
   The current implementation does not use it. It is kept for forward compatibility (a future
   plan may add configuration options such as enabling/disabling stages).

5. **`_collect_stale_warnings_from_content` vs re-importing from `query_engine`**: To avoid the
   circular import, the regex-based helper is duplicated locally in `query_cache.py` rather than
   importing from `query_engine.py`. The two implementations are identical and will stay in sync.
   A future refactor could extract this helper to `vault.py` or a shared utility module, but that
   is out of scope.

6. **`index_descriptions` is passed into both stage helpers**: Pre-computing it once in
   `check_query_cache()` avoids two separate file reads of `index.md`. Both stages use the same
   dict.

7. **Slug walk termination**: The walk terminates as soon as a candidate path does NOT exist on
   disk. This matches the `_unique_query_path()` dedup scheme exactly: if `slug.md`, `slug-2.md`,
   and `slug-3.md` exist but `slug-4.md` does not, the walk stops after checking `slug-3.md`.
   Files are not skipped within the sequence.

8. **`cached_path` is a vault-relative `Path`**: The spec (FR-QC-4) says
   `cached_path: Path | None` with the example `Path("queries/how-does-auth-work.md")`. Construct
   it as `Path(vault_rel.as_posix())` where `vault_rel = file_path.relative_to(vault_root)`.
   Do NOT return an absolute path.
