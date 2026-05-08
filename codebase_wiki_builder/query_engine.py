"""Query engine for Codebase Wiki Builder.

Implements the two-LLM-call query workflow described in FR-5:
  1. First call: identify relevant summary files from index.md (JSON array).
  2. Second call: answer the question using those summaries within a token budget.

Public API:
  - QUERY_CONTEXT_WINDOW: int constant (128,000 tokens)
  - NoRelevantFilesError: exception raised when LLM returns no relevant files
  - QueryResult: dataclass returned by run_query()
  - run_query(): main entry point — raises NoRelevantFilesError or LLMError

No typer imports. This is a pure logic module; the CLI layer handles all
transport-specific concerns.
"""

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

from codebase_wiki_builder.query_cache import check_query_cache

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

QUERY_CONTEXT_WINDOW = 128_000  # tokens; hardcoded per spec

# ---------------------------------------------------------------------------
# Lazy tiktoken encoder
# ---------------------------------------------------------------------------

_encoder: "tiktoken.Encoding | None" = None


def _get_encoder() -> "tiktoken.Encoding":
    """Return the shared cl100k_base encoder, initialising it on first call."""
    global _encoder
    if _encoder is None:
        import tiktoken as _tiktoken
        _encoder = _tiktoken.get_encoding("cl100k_base")
    return _encoder


def _count_tokens(text: str) -> int:
    """Estimate token count for *text* using tiktoken cl100k_base."""
    return len(_get_encoder().encode(text))


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class NoRelevantFilesError(Exception):
    """Raised by run_query() when the LLM returns no relevant files for the question."""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class QueryResult:
    """Result of a successful run_query() call."""

    answer: str
    """The full answer text including the ## Sources section."""

    sources: list[str]
    """Vault-relative paths of included summary files (e.g. ["src/auth/login.py.md"])."""

    one_line_summary: str
    """LLM-generated one-line description for index.md."""

    stale_warnings: list[str] = field(default_factory=list)
    """Vault-relative paths of query pages currently flagged as stale. Empty list if none."""

    from_cache: bool = False
    """True if the result was returned from a saved page without running LLM calls."""

    cached_path: Path | None = None
    """Vault-relative path of the matched cache page (e.g. Path("queries/how-does-auth-work.md")).
    None on fresh (non-cached) results."""

    cached_at: str | None = None
    """The saved_at timestamp string from the matched page's ## Page Metadata section.
    None on fresh results, or when saved_at is absent/unparseable in the cached page."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Matches a wikilink followed (anywhere on the same line) by the stale marker.
_STALE_ROW_RE = re.compile(r"\[\[([^\]]+)\]\].*⚠ stale")


def _collect_stale_warnings(index_content: str) -> list[str]:
    """Return vault-relative paths of stale query pages from index.md.

    Scans index.md for rows containing ' ⚠ stale' in the Description column.
    Returns vault-relative file paths (re-adding the .md extension that the
    wikilink omits).

    Example: a row ``| [[queries/how-auth-works]] | Explains auth ⚠ stale |``
    yields ``"queries/how-auth-works.md"``.
    """
    warnings: list[str] = []
    for line in index_content.splitlines():
        m = _STALE_ROW_RE.search(line)
        if m:
            path = m.group(1) + ".md"
            warnings.append(path)
    return warnings


def _build_relevance_prompt(question: str, index_content: str) -> str:
    """Build the first-LLM-call prompt using an f-string.

    Uses f-string concatenation rather than .format() so that any curly braces
    in untrusted content (source-file snippets, user question) cannot corrupt
    the prompt.
    """
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


def _parse_relevance_response(raw: str) -> list[str]:
    """Parse a JSON array of file paths from the LLM relevance response.

    Returns an empty list if parsing fails — the caller will raise
    NoRelevantFilesError on an empty list.
    """
    raw = raw.strip()

    # Try the whole response first
    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return [str(p) for p in result]
    except json.JSONDecodeError:
        pass

    # Fall back: extract the first [...] block in case the model added preamble
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


def _fill_context_budget(
    relevant_paths: list[str],
    vault_root: Path,
    log: logging.Logger,
) -> tuple[list[tuple[str, str]], list[str], int]:
    """Fill context up to QUERY_CONTEXT_WINDOW tokens from the top of the relevance list.

    Processes *relevant_paths* in order (highest relevance first).

    Returns:
        included: list of (vault_relative_path, file_content) pairs that fit
        too_large: list of vault-relative paths whose file exceeds the entire budget
        overflow_count: number of files skipped because the cumulative budget was exhausted
    """
    included: list[tuple[str, str]] = []
    too_large: list[str] = []
    overflow_count = 0
    tokens_used = 0

    for rel_path in relevant_paths:
        # Guard against LLM-returned absolute paths
        safe_rel = rel_path.lstrip("/")
        summary_path = vault_root / safe_rel

        # Index wikilinks omit .md (Obsidian convention), so the LLM may return
        # paths without it. Try appending .md if the bare path doesn't exist.
        if not summary_path.exists():
            candidate = vault_root / (safe_rel + ".md")
            if candidate.exists():
                safe_rel = safe_rel + ".md"
                summary_path = candidate
            else:
                log.warning("Relevant summary not found in vault: %s", safe_rel)
                continue

        try:
            content = summary_path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("Cannot read summary %s: %s", safe_rel, exc)
            continue

        file_tokens = _count_tokens(content)

        if file_tokens > QUERY_CONTEXT_WINDOW:
            # Single file exceeds the entire budget — skip with annotation
            log.warning(
                "Summary %s is too large to include (%d tokens > %d budget)",
                safe_rel,
                file_tokens,
                QUERY_CONTEXT_WINDOW,
            )
            too_large.append(safe_rel)
            continue

        if tokens_used + file_tokens > QUERY_CONTEXT_WINDOW:
            # Cumulative budget exhausted — this file (and subsequent ones) overflow
            overflow_count += 1
            continue

        included.append((safe_rel, content))
        tokens_used += file_tokens

    return included, too_large, overflow_count


def _build_answer_prompt(
    question: str,
    included_summaries: list[tuple[str, str]],
) -> str:
    """Build the second-LLM-call prompt using an f-string.

    Uses f-string concatenation rather than .format() so that curly braces in
    untrusted summary content cannot corrupt the prompt.
    """
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


def _extract_first_sentence(text: str) -> str:
    """Extract the first sentence as a fallback one-line summary (max 120 chars)."""
    sentence = re.split(r"[.!?]", text.strip(), maxsplit=1)[0].strip()
    return sentence[:120] if sentence else "Query answer"


def _parse_answer_response(raw: str) -> tuple[str, str]:
    """Parse the two-field JSON response from the answer LLM call.

    Returns (answer_text, one_line_summary).
    Falls back to (raw_text, first_sentence) if JSON parsing fails.
    """
    raw = raw.strip()

    # Try the whole response first
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and "answer" in obj:
            answer = str(obj.get("answer", raw))
            summary = str(obj.get("one_line_summary", _extract_first_sentence(answer)))
            return answer, summary
    except json.JSONDecodeError:
        pass

    # Fall back: extract the first {...} block
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


def _build_sources_section(
    included: list[tuple[str, str]],
    too_large: list[str],
    overflow_count: int,
) -> tuple[str, list[str]]:
    """Build the ## Sources section and return (sources_markdown, sources_list).

    sources_list contains vault-relative paths for ALL files referenced in the
    section (included + too_large).  Overflow files are NOT listed individually —
    their count is reported via the overflow note appended to the answer body.
    """
    lines = ["## Sources"]
    all_source_paths: list[str] = []

    for rel_path, _ in included:
        lines.append(f"- {rel_path}")
        all_source_paths.append(rel_path)

    for rel_path in too_large:
        lines.append(f"- {rel_path} (too large to include)")
        all_source_paths.append(rel_path)

    sources_section = "\n".join(lines)
    return sources_section, all_source_paths


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_query(
    question: str,
    vault_root: Path,
    llm_client: "LLMClient",
    config: "WikiConfig",
) -> QueryResult:
    """Run the full two-LLM-call query workflow.

    Steps:
      1. Check index.md exists (raises FileNotFoundError if not).
      2. Read index.md; collect stale_warnings.
      2.5. Cache pre-check: call check_query_cache(). If a non-stale cached answer is
           found, copy stale_warnings into the result and return immediately.
      3. First LLM call: identify relevant files as JSON array sorted by relevance descending.
      4. Raise NoRelevantFilesError if LLM returns empty array.
      5. Fill context budget using tiktoken (QUERY_CONTEXT_WINDOW = 128_000 tokens).
         - Skip files that exceed the budget by themselves → annotate as (too large to include).
         - Stop filling when budget would be exceeded → track overflow count.
      6. Second LLM call: answer question + one-line summary.
      7. Build ## Sources section and overflow note.
      8. Return QueryResult.

    Raises:
        FileNotFoundError: if index.md does not exist in vault_root.
        NoRelevantFilesError: if the LLM identifies no relevant files.
        LLMError: on fatal LLM API failures.
    """
    # Step 1: Verify index.md exists
    index_path = vault_root / "index.md"
    if not index_path.exists():
        raise FileNotFoundError(
            "The vault has no summaries. Run 'codewiki ingest' first."
        )

    # Step 2: Read index.md, collect stale warnings (must run BEFORE LLM calls)
    index_content = index_path.read_text(encoding="utf-8")
    stale_warnings = _collect_stale_warnings(index_content)

    # Step 2.5: Cache pre-check — return saved answer if available and not stale
    cache_result = check_query_cache(question, vault_root, index_content, llm_client, config)
    if cache_result is not None:
        cache_result.stale_warnings = stale_warnings
        return cache_result

    # Step 3: First LLM call — relevance identification
    # LLMError propagates to caller on fatal failure
    relevance_prompt = _build_relevance_prompt(question, index_content)
    raw_relevance = llm_client.complete(relevance_prompt)
    relevant_paths = _parse_relevance_response(raw_relevance)
    logger.debug("Relevance response: %d path(s) identified", len(relevant_paths))

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

    # Step 8: Assemble final answer and return
    full_answer = answer_text + overflow_note + "\n\n" + sources_section

    return QueryResult(
        answer=full_answer,
        sources=all_source_paths,
        one_line_summary=one_line_summary,
        stale_warnings=stale_warnings,
    )
