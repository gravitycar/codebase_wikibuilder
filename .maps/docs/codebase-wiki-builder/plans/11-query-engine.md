# Implementation Plan: Query Core Logic

## Spec Context

This plan implements the shared query workflow that powers both the CLI `query` command (item 13) and the MCP server `wiki_query` tool (item 15). It fulfills FR-5's two-LLM-call approach: the first call identifies relevant summary files from `index.md` (returning a JSON array sorted by relevance descending), and the second call answers the question using those summaries within a 128,000-token budget. The module also checks for stale query pages at startup, handles oversized files and budget overflow, and formats the `## Sources` section. The `QueryResult` dataclass is the shared output contract consumed by both callers.

Catalog item: 11 — Query Core Logic
Specification section: FR-5 (all: stale warning check, empty-vault error, relevance identification via JSON array sorted by relevance, token budget filling from top of list, oversized-file skip, overflow note, `## Sources` section, one-line summary for index.md), Technical Context (`QUERY_CONTEXT_WINDOW = 128_000`, `tiktoken`)
Acceptance criteria addressed: AT-7 (query command), AT-11 (oversized summary skip), AT-12 (context overflow truncation)

## Dependencies

- **Blocked by**: Item 3 (LLM Client Abstraction) — needs `LLMClient`, `LLMError`
- **Blocked by**: Item 4 (Vault File Utilities + Logging) — needs `append_log_md()`, `wikilink()`
- **Blocked by**: Item 8 (Index + Staleness) — needs `index.md` to exist; needs `_parse_existing_index()` pattern for stale row detection
- **Blocks**: Item 12 (Query Page Persistence) — needs `QueryResult` dataclass
- **Blocks**: Item 13 (Query CLI) — calls `run_query()`
- **Blocks**: Item 15 (MCP Server) — calls `run_query()`
- **Uses**: `tiktoken` (token counting), `json` (stdlib), `re` (stdlib), `pathlib` (stdlib), `logging` (stdlib), `dataclasses` (stdlib)

## File Changes

### New Files

- `codebase_wiki_builder/query_engine.py` — `QueryResult` dataclass, `run_query()`, all token-budget logic, relevance sorting, sources annotation, stale warning check

### Modified Files

- None

---

## Implementation Details

### `NoRelevantFilesError` Exception

**File**: `codebase_wiki_builder/query_engine.py`

```python
class NoRelevantFilesError(Exception):
    """Raised by run_query() when the LLM returns no relevant files for the question."""
```

This exception is raised instead of `typer.Exit(code=3)` so that both the CLI (item 13) and the MCP server (item 15) can catch it and handle it in their own transport-appropriate way. `run_query()` does not import or use `typer` at all.

---

### `QueryResult` Dataclass

**File**: `codebase_wiki_builder/query_engine.py`

```python
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class QueryResult:
    answer: str
    """The full answer text including the ## Sources section."""

    sources: list[str]
    """Vault-relative paths of included summary files (e.g. ["src/auth/login.py.md"])."""

    one_line_summary: str
    """LLM-generated one-line description for index.md (e.g. "Explains how JWT auth works")."""

    stale_warnings: list[str]
    """Vault-relative paths of query pages currently flagged as stale. Empty list if none."""
```

`stale_warnings` is always a list (never `None`). Callers that want the MCP `list[str]|null` shape convert `[] → null` themselves. The `answer` field includes the full formatted `## Sources` section as a trailing section — callers print or save it as-is.

---

### Module-Level Constants

```python
QUERY_CONTEXT_WINDOW = 128_000  # tokens; hardcoded per spec
```

The `tiktoken` encoder is initialized lazily (on first use) to avoid importing `tiktoken` at module load time.

```python
_encoder: tiktoken.Encoding | None = None

def _get_encoder() -> tiktoken.Encoding:
    global _encoder
    if _encoder is None:
        import tiktoken
        _encoder = tiktoken.get_encoding("cl100k_base")
    return _encoder


def _count_tokens(text: str) -> int:
    return len(_get_encoder().encode(text))
```

`cl100k_base` is the encoding used by GPT-4 and Claude models for tiktoken estimation. It is used for budget estimation only — not for exact counts — so the specific encoding is an implementation detail rather than a correctness requirement.

---

### `run_query()` — Main Entry Point

**Signature**:

```python
def run_query(
    question: str,
    vault_root: Path,
    llm_client: LLMClient,
    config: WikiConfig,
) -> QueryResult:
    """Run the full two-LLM-call query workflow.

    Steps:
      1. Check index.md exists (exit code 1 if not).
      2. Read index.md; collect stale_warnings.
      3. First LLM call: identify relevant files as JSON array sorted by relevance descending.
      4. Exit with code 3 if LLM returns empty array.
      5. Fill context budget using tiktoken (QUERY_CONTEXT_WINDOW = 128_000 tokens).
         - Skip files that exceed the budget by themselves → annotate as (too large to include).
         - Stop filling when budget would be exceeded → track overflow count.
      6. Second LLM call: answer question + one-line summary.
      7. Build ## Sources section.
      8. Return QueryResult.
    """
```

---

### Step 1 — Verify `index.md` Exists

```python
index_path = vault_root / "index.md"
if not index_path.exists():
    raise FileNotFoundError(
        "The vault has no summaries. Run 'codewiki ingest' first."
    )
```

`run_query()` raises `FileNotFoundError` (with an informative message) when `index.md` is missing. The CLI caller (item 13) catches it and calls `typer.Exit(code=1)` with the message printed to stderr. The MCP server (item 15) catches it and returns a structured MCP error response. `run_query()` itself does not import or use `typer`.

---

### Step 2 — Read `index.md` and Collect Stale Warnings

```python
index_content = index_path.read_text(encoding="utf-8")
stale_warnings = _collect_stale_warnings(index_content)
```

The stale-warning check runs at the START, before any LLM calls. `stale_warnings` is returned in `QueryResult` for the caller to display or pass through.

**`_collect_stale_warnings(index_content: str) -> list[str]`**:

Scans `index.md` for rows containing ` ⚠ stale` in the Description column. Returns vault-relative file paths (the content of the wikilink, with `.md` extension re-added since the wikilink omits it).

```python
_STALE_ROW_RE = re.compile(r"\[\[([^\]]+)\]\].*⚠ stale")


def _collect_stale_warnings(index_content: str) -> list[str]:
    """Return vault-relative paths of stale query pages from index.md."""
    warnings = []
    for line in index_content.splitlines():
        m = _STALE_ROW_RE.search(line)
        if m:
            # wikilink target omits .md; add it back for the path
            path = m.group(1) + ".md"
            warnings.append(path)
    return warnings
```

Example: a row `| [[queries/how-auth-works]] | Explains auth ⚠ stale |` yields `"queries/how-auth-works.md"`.

---

### Step 3 — First LLM Call: Relevance Identification

**Prompt construction**:

```python
def _build_relevance_prompt(question: str, index_content: str) -> str:
    return (
        "You are a search assistant for a codebase wiki. "
        "Given the index below and a question, identify which wiki pages are relevant to answering the question.\n\n"
        "Return ONLY a JSON array of vault-relative file paths, sorted by relevance descending "
        "(most relevant first). Include only pages that are genuinely relevant. "
        "If no pages are relevant, return an empty array [].\n\n"
        "Do not include any explanation or text outside the JSON array.\n\n"
        f"Question: {question}\n\n"
        f"Wiki Index:\n{index_content}"
    )
```

**LLM call and JSON parsing**:

```python
relevance_prompt = _build_relevance_prompt(question, index_content)
raw_response = llm_client.complete(relevance_prompt)
relevant_paths = _parse_relevance_response(raw_response)
```

**`_parse_relevance_response(raw: str) -> list[str]`**:

Extracts the JSON array from the LLM response. The LLM is instructed to return only JSON, but defensively extract the first `[...]` block in case the model adds surrounding text.

```python
def _parse_relevance_response(raw: str) -> list[str]:
    """Parse a JSON array of file paths from the LLM relevance response.

    Returns an empty list if parsing fails.
    """
    # Try the whole response first
    raw = raw.strip()
    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return [str(p) for p in result]
    except json.JSONDecodeError:
        pass

    # Fall back: extract first [...] block
    bracket_match = re.search(r"\[.*?\]", raw, re.DOTALL)
    if bracket_match:
        try:
            result = json.loads(bracket_match.group(0))
            if isinstance(result, list):
                return [str(p) for p in result]
        except json.JSONDecodeError:
            pass

    logger.warning("Could not parse relevance response as JSON array: %r", raw[:200])
    return []
```

---

### Step 4 — Raise `NoRelevantFilesError` on Empty Array

```python
if not relevant_paths:
    raise NoRelevantFilesError("No relevant files found for that query.")
```

Per spec: "If the LLM returns an empty JSON array (zero relevant summaries identified), the application SHALL print 'No relevant files found for that query.' and exit with code 3." The core module raises `NoRelevantFilesError` — a clean Python exception with no CLI framework dependency. The CLI (item 13) catches this exception and calls `raise typer.Exit(code=3)` after printing the message. The MCP server (item 15) catches it and returns a structured MCP error response. `run_query()` never imports or calls `typer`.

---

### Step 5 — Fill Context Budget

**Context budget filling algorithm**:

```python
def _fill_context_budget(
    relevant_paths: list[str],
    vault_root: Path,
    logger: logging.Logger,
) -> tuple[list[tuple[str, str]], list[str], int]:
    """Fill context up to QUERY_CONTEXT_WINDOW tokens from top of relevance-sorted list.

    Returns:
        included: list of (vault_relative_path, file_content) pairs
        too_large: list of vault-relative paths that exceeded budget by themselves
        overflow_count: number of files skipped due to budget exhaustion (not too_large)
    """
    included: list[tuple[str, str]] = []
    too_large: list[str] = []
    overflow_count = 0
    tokens_used = 0

    for rel_path in relevant_paths:
        summary_path = vault_root / rel_path
        if not summary_path.exists():
            logger.warning("Relevant summary not found in vault: %s", rel_path)
            continue

        try:
            content = summary_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Cannot read summary %s: %s", rel_path, exc)
            continue

        file_tokens = _count_tokens(content)

        if file_tokens > QUERY_CONTEXT_WINDOW:
            # Single file exceeds the entire budget — skip with annotation
            logger.warning(
                "Summary %s is too large to include (%d tokens > %d budget)",
                rel_path, file_tokens, QUERY_CONTEXT_WINDOW,
            )
            too_large.append(rel_path)
            continue

        if tokens_used + file_tokens > QUERY_CONTEXT_WINDOW:
            # Budget exhausted — count remaining files as overflow
            overflow_count += 1
            continue

        included.append((rel_path, content))
        tokens_used += file_tokens

    # Count remaining relevant_paths after the last included file as overflow
    # (the loop above already increments overflow_count for each skipped file)

    return included, too_large, overflow_count
```

**Important**: the relevance-sorted list is processed top-to-bottom (highest relevance first). Once the budget fills, all remaining files that are not `too_large` are counted as overflow. The `too_large` files are skipped even if they appear early in the relevance list — they cannot be included regardless of budget position.

---

### Step 6 — Second LLM Call: Answer + One-Line Summary

**Prompt construction**:

```python
def _build_answer_prompt(question: str, included_summaries: list[tuple[str, str]]) -> str:
    summaries_block = "\n\n---\n\n".join(
        f"File: {rel_path}\n\n{content}"
        for rel_path, content in included_summaries
    )
    return (
        "You are a technical assistant answering questions about a codebase based on its wiki summaries.\n\n"
        "Answer the question below using only the provided wiki summaries. "
        "Cite which files informed your answer.\n\n"
        "Return your response as a JSON object with exactly two fields:\n"
        '  "answer": the full answer text (markdown-formatted)\n'
        '  "one_line_summary": a single sentence describing what the answer covers, '
        "suitable for a wiki index entry (e.g., 'Explains how the authentication middleware validates JWT tokens')\n\n"
        "Do not include any text outside the JSON object.\n\n"
        f"Question: {question}\n\n"
        f"Wiki Summaries:\n{summaries_block}"
    )
```

**LLM call and response parsing**:

```python
answer_prompt = _build_answer_prompt(question, included_summaries)
raw_answer = llm_client.complete(answer_prompt)
answer_text, one_line_summary = _parse_answer_response(raw_answer)
```

**`_parse_answer_response(raw: str) -> tuple[str, str]`**:

Extracts `answer` and `one_line_summary` from the JSON object. Falls back gracefully if the model doesn't return valid JSON.

```python
def _parse_answer_response(raw: str) -> tuple[str, str]:
    """Parse the two-field JSON response from the answer LLM call.

    Returns (answer_text, one_line_summary).
    Falls back to (raw_text, first_sentence) if JSON parsing fails.
    """
    raw = raw.strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and "answer" in obj:
            answer = str(obj.get("answer", raw))
            summary = str(obj.get("one_line_summary", _extract_first_sentence(answer)))
            return answer, summary
    except json.JSONDecodeError:
        pass

    # Fall back: extract first {...} block
    brace_match = re.search(r"\{.*\}", raw, re.DOTALL)
    if brace_match:
        try:
            obj = json.loads(brace_match.group(0))
            if isinstance(obj, dict) and "answer" in obj:
                answer = str(obj.get("answer", raw))
                summary = str(obj.get("one_line_summary", _extract_first_sentence(answer)))
                return answer, summary
        except json.JSONDecodeError:
            pass

    # Last resort: treat entire response as answer text
    logger.warning("Could not parse answer response as JSON; using raw text")
    return raw, _extract_first_sentence(raw)


def _extract_first_sentence(text: str) -> str:
    """Extract first sentence as a fallback one-line summary (max 120 chars)."""
    sentence = re.split(r"[.!?]", text.strip(), maxsplit=1)[0].strip()
    return sentence[:120] if sentence else "Query answer"
```

---

### Step 7 — Build `## Sources` Section and Overflow Note

```python
def _build_sources_section(
    included: list[tuple[str, str]],
    too_large: list[str],
    overflow_count: int,
) -> tuple[str, list[str]]:
    """Build the ## Sources section and return (sources_markdown, sources_list).

    sources_list: vault-relative paths of ALL relevant files (included + too_large),
                  used to populate QueryResult.sources for the query page's ## Sources.
    """
    lines = ["## Sources"]
    all_source_paths = []

    for rel_path, _ in included:
        lines.append(f"- {rel_path}")
        all_source_paths.append(rel_path)

    for rel_path in too_large:
        lines.append(f"- {rel_path} (too large to include)")
        all_source_paths.append(rel_path)

    sources_section = "\n".join(lines)
    return sources_section, all_source_paths
```

**Overflow note** — appended to the answer body (before `## Sources`):

```python
overflow_note = ""
if overflow_count > 0:
    overflow_note = (
        f"\n\n{overflow_count} additional relevant file(s) were found "
        "but omitted due to context limits."
    )
```

---

### Step 8 — Assemble Final Answer and Return `QueryResult`

```python
sources_section, all_source_paths = _build_sources_section(
    included_summaries, too_large, overflow_count
)

full_answer = answer_text + overflow_note + "\n\n" + sources_section

return QueryResult(
    answer=full_answer,
    sources=all_source_paths,
    one_line_summary=one_line_summary,
    stale_warnings=stale_warnings,
)
```

The `answer` field contains the complete formatted response (answer body + overflow note + `## Sources`) ready to print or save.

---

### Complete `run_query()` Body

```python
def run_query(
    question: str,
    vault_root: Path,
    llm_client: "LLMClient",
    config: "WikiConfig",
) -> QueryResult:
    # Step 1: Verify index.md exists
    index_path = vault_root / "index.md"
    if not index_path.exists():
        raise FileNotFoundError(
            "The vault has no summaries. Run 'codewiki ingest' first."
        )

    # Step 2: Read index.md, collect stale warnings
    index_content = index_path.read_text(encoding="utf-8")
    stale_warnings = _collect_stale_warnings(index_content)

    # Step 3: First LLM call — relevance identification
    # LLMError propagates to caller on fatal failure
    relevance_prompt = _build_relevance_prompt(question, index_content)
    raw_relevance = llm_client.complete(relevance_prompt)
    relevant_paths = _parse_relevance_response(raw_relevance)
    logger.debug("Relevance response: %d paths identified", len(relevant_paths))

    # Step 4: Raise NoRelevantFilesError if no relevant files found
    if not relevant_paths:
        raise NoRelevantFilesError("No relevant files found for that query.")

    # Step 5: Fill context budget
    included_summaries, too_large, overflow_count = _fill_context_budget(
        relevant_paths, vault_root, logger
    )

    # Step 6: Second LLM call — answer + one-line summary
    # LLMError propagates to caller on fatal failure
    answer_prompt = _build_answer_prompt(question, included_summaries)
    raw_answer = llm_client.complete(answer_prompt)
    answer_text, one_line_summary = _parse_answer_response(raw_answer)

    # Step 7: Build sources section and overflow note
    sources_section, all_source_paths = _build_sources_section(
        included_summaries, too_large, overflow_count
    )
    overflow_note = ""
    if overflow_count > 0:
        overflow_note = (
            f"\n\n{overflow_count} additional relevant file(s) were found "
            "but omitted due to context limits."
        )

    # Step 8: Assemble and return
    full_answer = answer_text + overflow_note + "\n\n" + sources_section
    return QueryResult(
        answer=full_answer,
        sources=all_source_paths,
        one_line_summary=one_line_summary,
        stale_warnings=stale_warnings,
    )
```

---

### Complete Module Skeleton

```python
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import tiktoken
    from codebase_wiki_builder.config import WikiConfig
    from codebase_wiki_builder.llm_client import LLMClient

logger = logging.getLogger(__name__)

QUERY_CONTEXT_WINDOW = 128_000

_encoder: "tiktoken.Encoding | None" = None


class NoRelevantFilesError(Exception):
    """Raised when the LLM returns no relevant files for the question."""


@dataclass
class QueryResult:
    answer: str
    sources: list[str]
    one_line_summary: str
    stale_warnings: list[str] = field(default_factory=list)


def run_query(
    question: str,
    vault_root: Path,
    llm_client: "LLMClient",
    config: "WikiConfig",
) -> QueryResult: ...


# Internal helpers
def _get_encoder() -> "tiktoken.Encoding": ...
def _count_tokens(text: str) -> int: ...
def _collect_stale_warnings(index_content: str) -> list[str]: ...
def _build_relevance_prompt(question: str, index_content: str) -> str: ...
def _parse_relevance_response(raw: str) -> list[str]: ...
def _fill_context_budget(
    relevant_paths: list[str],
    vault_root: Path,
    logger: logging.Logger,
) -> tuple[list[tuple[str, str]], list[str], int]: ...
def _build_answer_prompt(
    question: str,
    included_summaries: list[tuple[str, str]],
) -> str: ...
def _parse_answer_response(raw: str) -> tuple[str, str]: ...
def _build_sources_section(
    included: list[tuple[str, str]],
    too_large: list[str],
    overflow_count: int,
) -> tuple[str, list[str]]: ...
def _extract_first_sentence(text: str) -> str: ...
```

---

## Error Handling

| Condition | Behavior |
|-----------|----------|
| `index.md` does not exist | Raises `FileNotFoundError` with message "The vault has no summaries…"; caller handles |
| `index.md` unreadable (`OSError`) | Propagates to caller — unrecoverable |
| First LLM call fails (`LLMError`) | Propagates to caller (CLI catches and exits 1; MCP returns error) |
| First LLM response not parseable as JSON array | `_parse_relevance_response()` returns `[]`; triggers `NoRelevantFilesError` |
| Relevant summary file not found in vault | Logged at WARNING; skipped (path from LLM may reference stale index) |
| Relevant summary unreadable (`OSError`) | Logged at WARNING; skipped |
| Single summary too large (> 128,000 tokens) | Added to `too_large` list; annotated `(too large to include)` in `## Sources` |
| Budget fills before all relevant files included | Remaining files counted as `overflow_count`; note appended to answer |
| Second LLM call fails (`LLMError`) | Propagates to caller |
| Second LLM response not parseable as JSON | Falls back to raw text as answer; `_extract_first_sentence()` as summary |
| Empty `relevant_paths` after JSON parse | Raises `NoRelevantFilesError("No relevant files found for that query.")` |

---

## Unit Test Specifications

**File**: `tests/test_query_engine.py`

All tests use `tmp_path`. LLM calls mocked via `unittest.mock`. No real network calls.

---

### `QueryResult` Dataclass

| Case | Input | Expected | Why |
|------|-------|----------|-----|
| Construct with all fields | Provide all fields | Fields accessible with correct values | Basic contract |
| `stale_warnings` defaults to `[]` | Omit `stale_warnings` | Empty list, not None | MCP conversion safety |

---

### `run_query()` — `index.md` guard

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| `index.md` missing | Fresh vault dir, no `index.md` | `FileNotFoundError` raised with message containing "no summaries" | FR-5: empty vault error |
| `index.md` exists | Create `index.md` | Proceeds to LLM calls | Happy path |

---

### `run_query()` — stale warnings

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| No stale rows | `index.md` with no ` ⚠ stale` | `result.stale_warnings == []` | No stale pages |
| One stale row | `index.md` row with `⚠ stale` | `result.stale_warnings == ["queries/page.md"]` | Stale check at start |
| Multiple stale rows | Two stale rows | Both paths in `stale_warnings` | Multiple |
| Stale check runs before LLM calls | Intercept LLM mock call order | `stale_warnings` populated from index, not LLM response | AT: stale check at START |

---

### `run_query()` — `NoRelevantFilesError`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| LLM returns `[]` | Mock first LLM call → `"[]"` | `NoRelevantFilesError` raised | FR-5: no relevant files |
| LLM returns unparseable text | Mock first LLM call → `"sorry, no files"` | `NoRelevantFilesError` raised | Parse failure → empty → error |
| LLM returns non-empty array | Mock first LLM call → `'["src/foo.py.md"]'` | Proceeds to context filling | Happy path |

---

### `_parse_relevance_response()`

| Case | Input | Expected | Why |
|------|-------|----------|-----|
| Clean JSON array | `'["src/a.py.md", "src/b.py.md"]'` | `["src/a.py.md", "src/b.py.md"]` | Happy path |
| Empty array | `"[]"` | `[]` | Empty → exit 3 trigger |
| JSON with preamble | `"Here are the files:\n[\"src/a.py.md\"]"` | `["src/a.py.md"]` | Defensive extraction |
| Invalid JSON | `"not json at all"` | `[]` | Graceful fallback |
| Array of non-strings | `"[1, 2, 3]"` | `["1", "2", "3"]` | `str()` coercion |

---

### `_fill_context_budget()` — happy path

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| All files fit | 3 small summaries; total tokens < 128,000 | All 3 in `included`; `overflow_count=0` | Budget not exhausted |
| Budget exactly reached | Files that sum to exactly 128,000 tokens | All included; no overflow | Boundary condition |
| Budget exceeded | 5 files; first 3 fit, last 2 would exceed | 3 in `included`; `overflow_count=2` | Overflow truncation |
| Files ordered by relevance | Paths in relevance order | Included files match first N from relevance list | Relevance-first fill |

---

### `_fill_context_budget()` — oversized file (AT-11)

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Single file exceeds full budget | One summary file > 128,000 tokens | `too_large=["file.md"]`; `included=[]`; exit code 0 (no crash) | AT-11 |
| Oversized file skipped early in list | Oversized file is most relevant (first in list) | Skipped; next relevant files considered | Oversized skip doesn't stop processing |
| Oversized file in `## Sources` | Oversized file present | `sources` list includes it with `(too large to include)` annotation | AT-11: annotated in Sources |

**Key Scenario: Oversized file annotated in Sources (AT-11)**

```python
def test_oversized_file_annotated_in_sources(tmp_path, monkeypatch):
    from codebase_wiki_builder.query_engine import run_query
    from unittest.mock import MagicMock

    vault = tmp_path / "vault"
    vault.mkdir()
    summaries_dir = vault / "src"
    summaries_dir.mkdir()

    # Create a summary file
    big_summary = summaries_dir / "big_file.py.md"
    big_summary.write_text("# big_file.py\n\n" + "x" * 100)

    # Create index.md
    (vault / "index.md").write_text(
        "| File | Description |\n|------|-------------|\n"
        "| [[src/big_file.py]] | Big file summary |\n"
    )

    # Mock tiktoken to report the file as oversized
    monkeypatch.setattr(
        "codebase_wiki_builder.query_engine._count_tokens",
        lambda text: 200_000,  # always > QUERY_CONTEXT_WINDOW
    )

    llm_client = MagicMock()
    # First call: relevance
    llm_client.complete.side_effect = [
        '["src/big_file.py.md"]',   # relevance response
        '{"answer": "Some answer.", "one_line_summary": "Summary"}',  # answer response
    ]

    config = MagicMock()
    result = run_query("How does big_file work?", vault, llm_client, config)

    # File annotated as too large in sources
    assert "src/big_file.py.md" in result.sources
    assert "(too large to include)" in result.answer
    # Not a crash — exit code 0 path
```

---

### `_fill_context_budget()` — overflow truncation (AT-12)

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Overflow note in answer | 5 relevant files; only 3 fit | Answer contains "2 additional relevant file(s) were found but omitted due to context limits." | AT-12 |
| Sources only lists included files | Same as above | `result.sources` has 3 paths (not 5) | AT-12: Sources = included only |
| Overflow count accurate | N files skipped | Note says exactly N | AT-12: X = count of omitted |

**Key Scenario: Overflow truncation (AT-12)**

```python
def test_overflow_truncation(tmp_path, monkeypatch):
    from codebase_wiki_builder.query_engine import run_query, QUERY_CONTEXT_WINDOW
    from unittest.mock import MagicMock

    vault = tmp_path / "vault"
    vault.mkdir()
    src = vault / "src"
    src.mkdir()

    # Create 5 small summary files
    paths = []
    for i in range(5):
        p = src / f"file{i}.py.md"
        p.write_text(f"# file{i}.py\n\nContent {i}.\n")
        paths.append(f"src/file{i}.py.md")

    (vault / "index.md").write_text(
        "| File | Description |\n|------|-------------|\n"
        + "\n".join(f"| [[src/file{i}.py]] | File {i} |\n" for i in range(5))
    )

    # Mock token counts: each file = 50,000 tokens → first 2 fit (100,000), third would exceed 128,000
    call_count = [0]
    def fake_count_tokens(text: str) -> int:
        call_count[0] += 1
        return 50_000  # each file = 50k tokens

    monkeypatch.setattr("codebase_wiki_builder.query_engine._count_tokens", fake_count_tokens)

    llm_client = MagicMock()
    relevance_response = json.dumps(paths)  # all 5 paths
    answer_response = '{"answer": "The answer.", "one_line_summary": "Answers about files."}'
    llm_client.complete.side_effect = [relevance_response, answer_response]

    config = MagicMock()
    result = run_query("What do files do?", vault, llm_client, config)

    # Only 2 files included (2 × 50k = 100k < 128k; 3rd would make 150k > 128k)
    assert len(result.sources) == 2
    assert "3 additional relevant file(s) were found but omitted due to context limits." in result.answer
```

---

### `_parse_answer_response()`

| Case | Input | Expected | Why |
|------|-------|----------|-----|
| Clean JSON | `'{"answer": "The answer.", "one_line_summary": "A summary."}'` | `("The answer.", "A summary.")` | Happy path |
| JSON with preamble | Extra text before `{` | Extracts from `{...}` block | Defensive fallback |
| Missing `one_line_summary` key | `'{"answer": "The answer."}'` | Returns answer + extracted first sentence | Graceful fallback |
| Invalid JSON entirely | Raw prose | Returns raw text + first sentence | Last resort |

---

### `_collect_stale_warnings()`

| Case | Input | Expected | Why |
|------|-------|----------|-----|
| No stale rows | Clean index content | `[]` | No stale pages |
| One stale row | Row with `⚠ stale` | `["queries/page.md"]` | Path extracted correctly |
| Multiple stale rows | Two stale rows | Both paths returned | Multiple |
| Non-query stale row | Summary file row with `⚠ stale` | Path included (staleness can apply to any page) | Defensive: return all stale paths |

---

### `_build_sources_section()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Only included files | 2 included, 0 too_large, 0 overflow | `## Sources\n- a.py.md\n- b.py.md` | Normal case |
| Too-large file annotated | 1 included, 1 too_large | too_large file has `(too large to include)` | AT-11 |
| No files at all | 0 included, 0 too_large | `## Sources` heading only | Edge case |

---

### Key Scenario: Full happy path

**Setup**:
- Vault with `index.md` listing two summary files
- Two summary files under `src/`
- Mock first LLM call → `'["src/auth.py.md", "src/utils.py.md"]'`
- Mock second LLM call → `'{"answer": "Auth uses JWT.", "one_line_summary": "Explains JWT auth."}'`
- Mock `_count_tokens` → 1000 per file (well within budget)

**Action**: `run_query("How does auth work?", vault, mock_llm, mock_config)`

**Expected**:
- `result.answer` starts with "Auth uses JWT." and ends with `## Sources` section
- `result.sources == ["src/auth.py.md", "src/utils.py.md"]`
- `result.one_line_summary == "Explains JWT auth."`
- `result.stale_warnings == []`
- Both LLM calls made in order (first for relevance, second for answer)

```python
def test_run_query_happy_path(tmp_path, monkeypatch):
    import json
    from codebase_wiki_builder.query_engine import run_query
    from unittest.mock import MagicMock, call

    vault = tmp_path / "vault"
    vault.mkdir()
    src = vault / "src"
    src.mkdir()

    (src / "auth.py.md").write_text("# src/auth.py\n\nHandles JWT authentication.\n")
    (src / "utils.py.md").write_text("# src/utils.py\n\nUtility functions.\n")
    (vault / "index.md").write_text(
        "| File | Description |\n|------|-------------|\n"
        "| [[src/auth.py]] | Auth module |\n"
        "| [[src/utils.py]] | Utils |\n"
    )

    monkeypatch.setattr("codebase_wiki_builder.query_engine._count_tokens", lambda t: 1000)

    llm_client = MagicMock()
    llm_client.complete.side_effect = [
        '["src/auth.py.md", "src/utils.py.md"]',
        '{"answer": "Auth uses JWT.", "one_line_summary": "Explains JWT auth."}',
    ]
    config = MagicMock()

    result = run_query("How does auth work?", vault, llm_client, config)

    assert result.one_line_summary == "Explains JWT auth."
    assert "Auth uses JWT." in result.answer
    assert "## Sources" in result.answer
    assert result.sources == ["src/auth.py.md", "src/utils.py.md"]
    assert result.stale_warnings == []
    assert llm_client.complete.call_count == 2
```

---

## Notes

- **`run_query()` does not import `typer`**: `query_engine.py` has no dependency on the CLI framework. `run_query()` raises standard Python exceptions: `FileNotFoundError` for a missing `index.md`, `NoRelevantFilesError` (defined in `query_engine.py`) for an empty relevance result, and `LLMError` for fatal LLM failures. Callers (CLI item 13, MCP server item 15) translate these exceptions into their respective transport-layer responses (`typer.Exit` or `McpError`).

- **`QueryResult.answer` includes the `## Sources` section**: The callers (CLI query command, MCP server, lint staleness resolver) all use `result.answer` as the complete page body. Having `## Sources` embedded in `result.answer` means callers can write the answer to disk without reassembling it. The `result.sources` list is separately available for programmatic use (e.g., MCP JSON response `sources` field, staleness detection in future ingest runs).

- **`_count_tokens()` uses `cl100k_base`**: This encoder is used for estimation only. The actual token count when the LLM processes the prompt may differ (model-specific tokenizers vary), but for budget purposes the difference is small enough that `cl100k_base` is a safe conservative estimate.

- **Overflow count vs. `too_large`**: These are distinct categories. `too_large` files are files that would exceed the entire 128,000-token budget by themselves; they are skipped regardless of how much budget is available. `overflow_count` files are files that would fit individually but were skipped because the cumulative budget was already exhausted. Both categories appear in `## Sources` (too_large with annotation; overflow files are NOT listed since they were never considered individually — only the count is reported in the overflow note).

- **Relevance path normalization**: The LLM returns paths as strings from the index. These paths are vault-relative. `_fill_context_budget()` resolves them as `vault_root / rel_path`. If the LLM returns a path with a leading `/` or wrong slashes, `vault_root / "/absolute/path"` in Python will resolve to the absolute path (losing vault_root). Guard this by stripping leading slashes: `rel_path = rel_path.lstrip("/")` before the join.

- **First LLM call prompt includes full `index.md`**: The index can be large in a well-populated vault. If the index itself exceeds the model's context window, the first LLM call will fail. For MVP this is acceptable — the spec does not require chunking the index. A future optimization would be to summarize the index or use embeddings for retrieval. For now, the index is passed whole.

- **Second LLM call may receive no summaries if all are too_large**: If `included_summaries` is empty (every relevant file is too large), the second LLM call receives no summary content. In this edge case the answer will be based only on the question itself (the LLM has no context). This is technically correct behavior — the sources section will list all files as `(too large to include)`. The application does not exit with code 3 in this case (code 3 is only for "no relevant files identified").

- **`stale_warnings` populated before LLM calls**: FR-5 requires the stale-page warning at the START of the query command, before answering. `run_query()` collects `stale_warnings` before making any LLM calls. The caller (CLI item 13) is responsible for displaying them to the terminal after `run_query()` returns; `run_query()` does not print them — it only returns them in `QueryResult`.

- **No `log.md` writes in `run_query()`**: The query engine is a pure computation module. All `log.md` writes (the `query` entry and `query-saved` entry) are the responsibility of the CLI caller (items 12 and 13). This keeps `query_engine.py` free of logging side effects and makes it easier to test.
