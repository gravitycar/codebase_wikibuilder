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

# ---------------------------------------------------------------------------
# Module-level regex constants
# ---------------------------------------------------------------------------

_STALE_ROW_RE = re.compile(r"\[\[([^\]]+)\]\].*⚠ stale")


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

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


def _strip_stale_suffix(description: str) -> str:
    """Remove the ' ⚠ stale' annotation from an index.md description if present."""
    return description.replace(" ⚠ stale", "").strip()


def _collect_stale_warnings_from_content(index_content: str) -> list[str]:
    """Derive stale_warnings list from index_content (mirrors query_engine logic)."""
    warnings = []
    for line in index_content.splitlines():
        m = _STALE_ROW_RE.search(line)
        if m:
            warnings.append(m.group(1) + ".md")
    return warnings


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


# ---------------------------------------------------------------------------
# Stage 1 — Slug walk
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Stage 2 — LLM pre-check
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

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
