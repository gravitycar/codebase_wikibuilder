"""Unit tests for codebase_wiki_builder.query_cache module.

Covers:
  - _normalize_question helper
  - _strip_stale_suffix helper
  - _collect_stale_warnings_from_content helper
  - _validate_stage2_path (SEC-3 checks: prefix, containment, allowlist)
  - _parse_stage2_response helper
  - _build_stage2_prompt helper
  - _stage1_slug_walk (slug hit, miss, stale, numeric suffix, empty slug, parse error)
  - _stage2_llm_precheck (no rows, no candidates, LLM match, LLM NO_MATCH, LLM error,
                          stale match, SEC-3 failures)
  - check_query_cache (Stage 1 hit skips Stage 2, Stage 1 miss runs Stage 2,
                       both miss, unexpected error, from_cache=True, cached_path,
                       cached_at, cached_at=None when saved_at missing,
                       stale_warnings propagated)

Acceptance tests addressed: AT-3 through AT-9, AT-12 through AT-17.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_wiki_builder.query_cache import (
    _build_stage2_prompt,
    _collect_stale_warnings_from_content,
    _normalize_question,
    _parse_stage2_response,
    _stage1_slug_walk,
    _stage2_llm_precheck,
    _strip_stale_suffix,
    _validate_stage2_path,
    check_query_cache,
)

# ---------------------------------------------------------------------------
# Helpers used throughout tests
# ---------------------------------------------------------------------------

def _write_query_page(
    path: Path,
    question: str = "How does auth work?",
    answer_body: str = "The answer.",
    sources: list[str] | None = None,
    saved_at: str = "2026-04-29 10:00:00 UTC",
    stale: bool = False,
) -> str:
    """Write a minimal but valid query page to *path* and return its content."""
    if sources is None:
        sources = ["src/auth.py.md"]
    stale_banner = "> [!warning] Stale Content\n> Source changed.\n\n" if stale else ""
    source_lines = "\n".join(f"- {s}" for s in sources)
    content = (
        f"# {question}\n\n"
        f"{stale_banner}"
        f"{answer_body}\n\n"
        f"## Sources\n{source_lines}\n\n"
        f"## Page Metadata\n"
        f"saved_at: {saved_at}\n"
        f"updated_at: {saved_at}\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return content


def _write_index(vault_root: Path, rows: list[tuple[str, str]]) -> None:
    """Write a minimal index.md to vault_root with the given (wikilink_target, desc) rows."""
    lines = ["| File | Description |", "|------|-------------|"]
    for target, desc in rows:
        lines.append(f"| [[{target}]] | {desc} |")
    (vault_root / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_index_descriptions(rows: list[tuple[str, str]]) -> dict[str, str]:
    """Build an index_descriptions dict from (wikilink_target, desc) rows."""
    return dict(rows)


# ---------------------------------------------------------------------------
# TestNormalizeQuestion
# ---------------------------------------------------------------------------

class TestNormalizeQuestion:
    def test_lowercases(self):
        assert _normalize_question("HOW DOES AUTH WORK") == "how does auth work"

    def test_strips_punctuation(self):
        assert _normalize_question("How does auth work?") == "how does auth work"

    def test_collapses_whitespace(self):
        assert _normalize_question("how  does   auth  work") == "how does auth work"

    def test_strips_leading_trailing(self):
        assert _normalize_question("  how does auth work  ") == "how does auth work"

    def test_combined(self):
        assert _normalize_question("  HOW DOES AUTH WORK?  ") == "how does auth work"

    def test_empty_string(self):
        assert _normalize_question("") == ""

    def test_only_punctuation(self):
        assert _normalize_question("???!") == ""

    def test_mixed_punctuation(self):
        # Colons, commas, apostrophes stripped
        result = _normalize_question("What's the auth module's role?")
        assert result == "whats the auth modules role"


# ---------------------------------------------------------------------------
# TestStripStaleSuffix
# ---------------------------------------------------------------------------

class TestStripStaleSuffix:
    def test_strips_stale_annotation(self):
        assert _strip_stale_suffix("Explains auth ⚠ stale") == "Explains auth"

    def test_passthrough_when_no_annotation(self):
        assert _strip_stale_suffix("Explains auth") == "Explains auth"

    def test_empty_string(self):
        assert _strip_stale_suffix("") == ""

    def test_strips_leading_trailing_spaces_after_removal(self):
        # " ⚠ stale" removal leaves trailing space; strip() handles it
        assert _strip_stale_suffix("Explains auth ⚠ stale") == "Explains auth"

    def test_no_partial_strip(self):
        # If the suffix is not exactly " ⚠ stale" it should remain
        result = _strip_stale_suffix("Explains stale auth")
        assert "stale" in result  # not stripped from wrong position


# ---------------------------------------------------------------------------
# TestCollectStaleWarningsFromContent
# ---------------------------------------------------------------------------

class TestCollectStaleWarningsFromContent:
    def test_no_stale_rows(self):
        content = "| [[queries/auth]] | Explains auth |\n"
        assert _collect_stale_warnings_from_content(content) == []

    def test_single_stale_row(self):
        content = "| [[queries/auth]] | Explains auth ⚠ stale |\n"
        result = _collect_stale_warnings_from_content(content)
        assert result == ["queries/auth.md"]

    def test_multiple_stale_rows(self):
        content = (
            "| [[queries/page-one]] | Desc ⚠ stale |\n"
            "| [[queries/page-two]] | Other ⚠ stale |\n"
            "| [[queries/page-three]] | Clean |\n"
        )
        result = _collect_stale_warnings_from_content(content)
        assert "queries/page-one.md" in result
        assert "queries/page-two.md" in result
        assert "queries/page-three.md" not in result
        assert len(result) == 2

    def test_empty_content(self):
        assert _collect_stale_warnings_from_content("") == []


# ---------------------------------------------------------------------------
# TestValidateStage2Path  (SEC-3)
# ---------------------------------------------------------------------------

class TestValidateStage2Path:
    def test_valid_path_passes(self, tmp_path):
        """A correctly formed path that is in valid_targets passes all three checks."""
        queries_dir = tmp_path / "queries"
        queries_dir.mkdir()
        # The actual file does not need to exist for the containment check —
        # resolve() works on non-existent paths in Python 3.6+ on most platforms.
        valid_targets = {"queries/auth"}
        result = _validate_stage2_path("queries/auth", tmp_path, valid_targets)
        assert result is True

    def test_prefix_check_rejects_non_queries_prefix(self, tmp_path):
        """Paths not starting with 'queries/' fail the prefix check (AT-15)."""
        valid_targets = {"summaries/auth"}
        for bad_path in ["summaries/auth", "../sensitive/file", "secrets", ""]:
            assert _validate_stage2_path(bad_path, tmp_path, valid_targets) is False

    def test_prefix_check_rejects_queries_without_slash(self, tmp_path):
        """'queries' without a trailing slash must fail."""
        valid_targets = {"queries"}
        assert _validate_stage2_path("queries", tmp_path, valid_targets) is False

    def test_containment_check_rejects_path_traversal(self, tmp_path):
        """queries/../../etc/passwd resolves outside vault_root/queries/ (AT-16)."""
        # Even if it's in the allowlist, the containment check must catch it.
        valid_targets = {"queries/../../etc/passwd"}
        result = _validate_stage2_path("queries/../../etc/passwd", tmp_path, valid_targets)
        assert result is False

    def test_containment_check_rejects_nested_traversal(self, tmp_path):
        """queries/sub/../../../outside also resolves outside."""
        valid_targets = {"queries/sub/../../../outside"}
        result = _validate_stage2_path("queries/sub/../../../outside", tmp_path, valid_targets)
        assert result is False

    def test_allowlist_check_rejects_unlisted_path(self, tmp_path):
        """Syntactically valid path not in pre-computed valid_targets fails (AT-17)."""
        queries_dir = tmp_path / "queries"
        queries_dir.mkdir()
        # "queries/other-page" NOT in valid_targets
        valid_targets = {"queries/auth"}
        result = _validate_stage2_path("queries/other-page", tmp_path, valid_targets)
        assert result is False

    def test_all_three_checks_required_prefix_blocks_first(self, tmp_path):
        """The prefix check is evaluated first; a non-queries path is rejected immediately."""
        # Even if it were in the allowlist, prefix check rejects
        valid_targets = {"summaries/auth"}
        assert _validate_stage2_path("summaries/auth", tmp_path, valid_targets) is False

    def test_empty_valid_targets_rejects_otherwise_valid_path(self, tmp_path):
        """An empty allowlist rejects every path."""
        queries_dir = tmp_path / "queries"
        queries_dir.mkdir()
        valid_targets: set[str] = set()
        assert _validate_stage2_path("queries/auth", tmp_path, valid_targets) is False


# ---------------------------------------------------------------------------
# TestParseStage2Response
# ---------------------------------------------------------------------------

class TestParseStage2Response:
    def test_no_match_returns_none(self):
        assert _parse_stage2_response("NO_MATCH") is None

    def test_no_match_case_insensitive(self):
        assert _parse_stage2_response("no_match") is None

    def test_empty_response_returns_none(self):
        assert _parse_stage2_response("") is None
        assert _parse_stage2_response("   ") is None

    def test_valid_path_returned(self):
        result = _parse_stage2_response("queries/how-does-auth-work")
        assert result == "queries/how-does-auth-work"

    def test_strips_trailing_punctuation(self):
        result = _parse_stage2_response("queries/how-does-auth-work.")
        assert result == "queries/how-does-auth-work"

    def test_strips_trailing_colon(self):
        result = _parse_stage2_response("queries/auth:")
        assert result == "queries/auth"

    def test_non_queries_prefix_returns_none(self):
        # Response that looks like a path but not under queries/
        assert _parse_stage2_response("summaries/auth") is None
        assert _parse_stage2_response("../etc/passwd") is None

    def test_whitespace_around_valid_path(self):
        result = _parse_stage2_response("  queries/auth-flow  ")
        assert result == "queries/auth-flow"

    def test_garbled_response_returns_none(self):
        assert _parse_stage2_response("I cannot determine a match from these entries.") is None

    def test_path_with_numeric_suffix(self):
        result = _parse_stage2_response("queries/how-does-auth-work-2")
        assert result == "queries/how-does-auth-work-2"


# ---------------------------------------------------------------------------
# TestBuildStage2Prompt
# ---------------------------------------------------------------------------

class TestBuildStage2Prompt:
    def test_contains_incoming_question(self):
        prompt = _build_stage2_prompt(
            "How does auth work?",
            [("queries/auth", "How does auth work?", "Explains auth")]
        )
        assert "How does auth work?" in prompt

    def test_contains_candidate_path(self):
        prompt = _build_stage2_prompt(
            "What is auth?",
            [("queries/auth", "How does auth work?", "Explains auth")]
        )
        assert "queries/auth" in prompt

    def test_contains_candidate_summary(self):
        prompt = _build_stage2_prompt(
            "What is auth?",
            [("queries/auth", "How does auth work?", "Explains auth")]
        )
        assert "Explains auth" in prompt

    def test_conservative_language_present(self):
        prompt = _build_stage2_prompt("Q?", [("queries/a", "Q?", "desc")])
        assert "HIGHLY CONFIDENT" in prompt
        assert "NO_MATCH" in prompt

    def test_multiple_candidates_included(self):
        candidates = [
            ("queries/auth", "How does auth work?", "Explains auth"),
            ("queries/db", "How does the DB work?", "Explains database"),
        ]
        prompt = _build_stage2_prompt("Explain auth", candidates)
        assert "queries/auth" in prompt
        assert "queries/db" in prompt


# ---------------------------------------------------------------------------
# TestStage1SlugWalk
# ---------------------------------------------------------------------------

STANDARD_INDEX_ROWS = [("queries/how-does-auth-work", "Explains auth")]


class TestStage1SlugWalk:

    def _run(self, question: str, vault_root: Path,
             stale_warnings: list[str] | None = None,
             index_descriptions: dict[str, str] | None = None):
        return _stage1_slug_walk(
            question=question,
            vault_root=vault_root,
            stale_warnings=stale_warnings or [],
            index_descriptions=index_descriptions or {},
        )

    def test_empty_slug_returns_none(self, tmp_path):
        """Empty slug (e.g., question is all punctuation) skips Stage 1 entirely."""
        result = self._run("???!!!", tmp_path)
        assert result is None

    def test_no_candidate_file_returns_none(self, tmp_path):
        """No file at all under queries/ — immediate None."""
        (tmp_path / "queries").mkdir()
        result = self._run("How does auth work?", tmp_path)
        assert result is None

    def test_slug_hit_exact_match(self, tmp_path):
        """Exact question match on queries/<slug>.md returns a cache hit (AT-1/AT-2)."""
        queries_dir = tmp_path / "queries"
        raw = _write_query_page(queries_dir / "how-does-auth-work.md")
        _write_index(tmp_path, STANDARD_INDEX_ROWS)
        index_descriptions = _make_index_descriptions(STANDARD_INDEX_ROWS)

        result = self._run("How does auth work?", tmp_path,
                           index_descriptions=index_descriptions)
        assert result is not None
        assert result.from_cache is True
        assert result.cached_path == Path("queries/how-does-auth-work.md")
        assert result.cached_at == "2026-04-29 10:00:00 UTC"
        assert result.answer == raw

    def test_slug_hit_case_insensitive_normalized(self, tmp_path):
        """Uppercase/different casing still matches the stored page (AT-2)."""
        queries_dir = tmp_path / "queries"
        _write_query_page(queries_dir / "how-does-auth-work.md")
        _write_index(tmp_path, STANDARD_INDEX_ROWS)
        index_descriptions = _make_index_descriptions(STANDARD_INDEX_ROWS)

        result = self._run("HOW DOES AUTH WORK?", tmp_path,
                           index_descriptions=index_descriptions)
        assert result is not None
        assert result.from_cache is True

    def test_slug_hit_numeric_suffix(self, tmp_path):
        """Finds match in <slug>-2.md when <slug>.md has a different H1 (AT-3)."""
        queries_dir = tmp_path / "queries"
        queries_dir.mkdir()
        _write_query_page(
            queries_dir / "how-does-auth-work.md",
            question="Some other question",
        )
        _write_query_page(
            queries_dir / "how-does-auth-work-2.md",
            question="How does auth work?",
        )
        rows = [
            ("queries/how-does-auth-work", "Other"),
            ("queries/how-does-auth-work-2", "Explains auth"),
        ]
        _write_index(tmp_path, rows)
        index_descriptions = _make_index_descriptions(rows)

        result = self._run("How does auth work?", tmp_path,
                           index_descriptions=index_descriptions)
        assert result is not None
        assert result.from_cache is True
        assert result.cached_path == Path("queries/how-does-auth-work-2.md")
        assert result.cached_at == "2026-04-29 10:00:00 UTC"

    def test_stale_hit_returns_none_full_miss(self, tmp_path):
        """Stage 1 finds a stale slug match — returns None, no sibling fallback (AT-4).

        Even though how-does-auth-work-2.md exists and is fresh, the stale primary
        is a full miss with no iteration.
        """
        queries_dir = tmp_path / "queries"
        queries_dir.mkdir()
        _write_query_page(
            queries_dir / "how-does-auth-work.md",
            question="How does auth work?",
            stale=True,
        )
        _write_query_page(
            queries_dir / "how-does-auth-work-2.md",
            question="How does auth work?",
            stale=False,
        )
        rows = [
            ("queries/how-does-auth-work", "Explains auth ⚠ stale"),
            ("queries/how-does-auth-work-2", "Explains auth"),
        ]
        _write_index(tmp_path, rows)
        index_descriptions = _make_index_descriptions(rows)

        result = self._run("How does auth work?", tmp_path,
                           index_descriptions=index_descriptions)
        # Stale primary = full miss; sibling NOT checked
        assert result is None

    def test_h1_mismatch_continues_walk(self, tmp_path):
        """H1 mismatch on slug.md continues to slug-2.md."""
        queries_dir = tmp_path / "queries"
        queries_dir.mkdir()
        _write_query_page(
            queries_dir / "how-does-auth-work.md",
            question="Something completely different",
        )
        _write_query_page(
            queries_dir / "how-does-auth-work-2.md",
            question="How does auth work?",
        )
        rows = [
            ("queries/how-does-auth-work", "Other"),
            ("queries/how-does-auth-work-2", "Explains auth"),
        ]
        index_descriptions = _make_index_descriptions(rows)

        result = self._run("How does auth work?", tmp_path,
                           index_descriptions=index_descriptions)
        assert result is not None
        assert result.cached_path == Path("queries/how-does-auth-work-2.md")

    def test_parse_failure_skips_candidate_continues(self, tmp_path):
        """read_query_page() raising an exception skips that candidate and continues (AT-11)."""
        queries_dir = tmp_path / "queries"
        queries_dir.mkdir()
        # Write a malformed file (no H1) and a valid -2 file
        bad_file = queries_dir / "how-does-auth-work.md"
        bad_file.write_text("no h1 here just random content", encoding="utf-8")
        _write_query_page(
            queries_dir / "how-does-auth-work-2.md",
            question="How does auth work?",
        )
        rows = [
            ("queries/how-does-auth-work", "Bad"),
            ("queries/how-does-auth-work-2", "Explains auth"),
        ]
        index_descriptions = _make_index_descriptions(rows)

        result = self._run("How does auth work?", tmp_path,
                           index_descriptions=index_descriptions)
        # Bad file is skipped; -2 should be found
        assert result is not None
        assert result.cached_path == Path("queries/how-does-auth-work-2.md")

    def test_answer_is_raw_content(self, tmp_path):
        """The answer field on a cache hit is the full raw file content (FR-QC-4)."""
        queries_dir = tmp_path / "queries"
        raw = _write_query_page(
            queries_dir / "how-does-auth-work.md",
            question="How does auth work?",
            answer_body="The detailed answer.",
        )
        index_descriptions = _make_index_descriptions(STANDARD_INDEX_ROWS)

        result = self._run("How does auth work?", tmp_path,
                           index_descriptions=index_descriptions)
        assert result is not None
        assert result.answer == raw

    def test_one_line_summary_from_index(self, tmp_path):
        """one_line_summary is taken from index_descriptions for the matched page."""
        queries_dir = tmp_path / "queries"
        _write_query_page(queries_dir / "how-does-auth-work.md")
        rows = [("queries/how-does-auth-work", "Explains authentication middleware")]
        index_descriptions = _make_index_descriptions(rows)

        result = self._run("How does auth work?", tmp_path,
                           index_descriptions=index_descriptions)
        assert result is not None
        assert result.one_line_summary == "Explains authentication middleware"

    def test_stale_suffix_stripped_from_one_line_summary(self, tmp_path):
        """The ⚠ stale annotation is stripped from one_line_summary even if present."""
        # This scenario can't happen (stale pages are rejected), but defensively:
        # We set up a non-stale page but with a stale annotation in the index
        queries_dir = tmp_path / "queries"
        _write_query_page(queries_dir / "how-does-auth-work.md", stale=False)
        rows = [("queries/how-does-auth-work", "Explains auth ⚠ stale")]
        index_descriptions = _make_index_descriptions(rows)

        result = self._run("How does auth work?", tmp_path,
                           index_descriptions=index_descriptions)
        assert result is not None
        assert "⚠" not in result.one_line_summary

    def test_stale_warnings_propagated(self, tmp_path):
        """stale_warnings list passed to Stage 1 is present on the returned result."""
        queries_dir = tmp_path / "queries"
        _write_query_page(queries_dir / "how-does-auth-work.md")
        index_descriptions = _make_index_descriptions(STANDARD_INDEX_ROWS)
        warnings = ["queries/some-other-page.md"]

        result = self._run("How does auth work?", tmp_path,
                           stale_warnings=warnings,
                           index_descriptions=index_descriptions)
        assert result is not None
        assert result.stale_warnings == ["queries/some-other-page.md"]

    def test_cached_at_none_when_saved_at_missing(self, tmp_path):
        """cached_at is None when the matched page has no saved_at field (OQ-3)."""
        queries_dir = tmp_path / "queries"
        queries_dir.mkdir()
        # Write a page without saved_at in Page Metadata
        content = (
            "# How does auth work?\n\n"
            "The answer.\n\n"
            "## Sources\n- src/auth.py.md\n\n"
            "## Page Metadata\n"
            "updated_at: 2026-04-29 10:00:00 UTC\n"
        )
        (queries_dir / "how-does-auth-work.md").write_text(content, encoding="utf-8")
        index_descriptions = _make_index_descriptions(STANDARD_INDEX_ROWS)

        result = self._run("How does auth work?", tmp_path,
                           index_descriptions=index_descriptions)
        assert result is not None
        assert result.from_cache is True
        assert result.cached_at is None  # saved_at was missing

    def test_sources_from_page(self, tmp_path):
        """sources field on the returned QueryResult matches the page's Sources section."""
        queries_dir = tmp_path / "queries"
        _write_query_page(
            queries_dir / "how-does-auth-work.md",
            sources=["src/auth.py.md", "src/utils.py.md"],
        )
        index_descriptions = _make_index_descriptions(STANDARD_INDEX_ROWS)

        result = self._run("How does auth work?", tmp_path,
                           index_descriptions=index_descriptions)
        assert result is not None
        assert "src/auth.py.md" in result.sources
        assert "src/utils.py.md" in result.sources

    def test_walk_stops_when_no_next_suffix(self, tmp_path):
        """Walk terminates when slug-N.md does not exist; slug-(N+1).md is never checked."""
        queries_dir = tmp_path / "queries"
        queries_dir.mkdir()
        # slug.md exists but H1 mismatches; slug-2.md does NOT exist; slug-3.md does
        _write_query_page(
            queries_dir / "how-does-auth-work.md",
            question="Different question",
        )
        _write_query_page(
            queries_dir / "how-does-auth-work-3.md",
            question="How does auth work?",
        )
        # No how-does-auth-work-2.md — walk stops there
        result = self._run("How does auth work?", tmp_path,
                           index_descriptions={})
        assert result is None  # walk stopped at missing -2; -3 is never reached


# ---------------------------------------------------------------------------
# TestStage2LlmPrecheck
# ---------------------------------------------------------------------------

def _make_query_page_content(
    question: str = "How does auth work?",
    answer_body: str = "The answer.",
    sources: list[str] | None = None,
    saved_at: str = "2026-04-29 10:00:00 UTC",
    stale: bool = False,
) -> str:
    """Return page content as a string (without writing to disk)."""
    if sources is None:
        sources = ["src/auth.py.md"]
    source_lines = "\n".join(f"- {s}" for s in sources)
    stale_banner = "> [!warning] Stale Content\n> Source changed.\n\n" if stale else ""
    return (
        f"# {question}\n\n"
        f"{stale_banner}"
        f"{answer_body}\n\n"
        f"## Sources\n{source_lines}\n\n"
        f"## Page Metadata\n"
        f"saved_at: {saved_at}\n"
        f"updated_at: {saved_at}\n"
    )


class TestStage2LlmPrecheck:

    def _run(self, question: str, vault_root: Path, llm_client,
             index_content: str = "",
             stale_warnings: list[str] | None = None,
             index_descriptions: dict[str, str] | None = None):
        return _stage2_llm_precheck(
            question=question,
            vault_root=vault_root,
            index_content=index_content,
            llm_client=llm_client,
            stale_warnings=stale_warnings or [],
            index_descriptions=index_descriptions or {},
        )

    def test_no_query_rows_skips_llm(self, tmp_path):
        """No query pages in index — Stage 2 returns None without calling LLM (AT-7)."""
        mock_llm = MagicMock()
        # Only source-file rows, no queries/ rows
        index_descriptions = {"src/auth.py": "Auth module"}

        result = self._run("How does auth work?", tmp_path, mock_llm,
                           index_descriptions=index_descriptions)
        assert result is None
        mock_llm.complete.assert_not_called()

    def test_no_parseable_candidates_skips_llm(self, tmp_path):
        """All query page files are unreadable — LLM is not called."""
        mock_llm = MagicMock()
        queries_dir = tmp_path / "queries"
        queries_dir.mkdir()
        # File doesn't exist — read_query_page raises OSError
        # (we reference it in index_descriptions but don't create the file)
        index_descriptions = {"queries/nonexistent": "Desc"}

        result = self._run("How does auth work?", tmp_path, mock_llm,
                           index_descriptions=index_descriptions)
        assert result is None
        mock_llm.complete.assert_not_called()

    def test_llm_returns_no_match(self, tmp_path):
        """LLM returns NO_MATCH — cache miss (AT-6 partial)."""
        mock_llm = MagicMock()
        mock_llm.complete.return_value = "NO_MATCH"
        queries_dir = tmp_path / "queries"
        _write_query_page(queries_dir / "how-does-auth-work.md")
        index_descriptions = {"queries/how-does-auth-work": "Explains auth"}

        result = self._run("Explain authentication please", tmp_path, mock_llm,
                           index_descriptions=index_descriptions)
        assert result is None
        mock_llm.complete.assert_called_once()

    def test_llm_returns_valid_path_hit(self, tmp_path):
        """LLM returns a valid matching path — cache hit returned (AT-5)."""
        mock_llm = MagicMock()
        mock_llm.complete.return_value = "queries/how-does-auth-work"
        queries_dir = tmp_path / "queries"
        _write_query_page(queries_dir / "how-does-auth-work.md")
        _write_index(tmp_path, [("queries/how-does-auth-work", "Explains auth")])
        index_descriptions = {"queries/how-does-auth-work": "Explains auth"}

        result = self._run("Explain authentication", tmp_path, mock_llm,
                           index_descriptions=index_descriptions)
        assert result is not None
        assert result.from_cache is True
        assert result.cached_path == Path("queries/how-does-auth-work.md")
        mock_llm.complete.assert_called_once()

    def test_llm_error_returns_none(self, tmp_path):
        """LLM raises an exception — treated as a cache miss, no exception propagates (AT-12)."""
        mock_llm = MagicMock()
        mock_llm.complete.side_effect = Exception("LLM unavailable")
        queries_dir = tmp_path / "queries"
        _write_query_page(queries_dir / "how-does-auth-work.md")
        index_descriptions = {"queries/how-does-auth-work": "Explains auth"}

        result = self._run("How does auth work?", tmp_path, mock_llm,
                           index_descriptions=index_descriptions)
        assert result is None  # no exception propagated

    def test_llm_error_logged_at_warning(self, tmp_path, caplog):
        """LLM error is logged at WARNING level (AT-12)."""
        mock_llm = MagicMock()
        mock_llm.complete.side_effect = Exception("LLM unavailable")
        queries_dir = tmp_path / "queries"
        _write_query_page(queries_dir / "how-does-auth-work.md")
        index_descriptions = {"queries/how-does-auth-work": "Explains auth"}

        with caplog.at_level(logging.WARNING, logger="codebase_wiki_builder.query_cache"):
            self._run("How does auth work?", tmp_path, mock_llm,
                      index_descriptions=index_descriptions)
        assert any("WARNING" in r.levelname or r.levelno >= logging.WARNING
                   for r in caplog.records)

    def test_stale_match_returns_none(self, tmp_path):
        """LLM returns a matching path but the page is stale — full cache miss (spec FR-QC-2.5d)."""
        mock_llm = MagicMock()
        mock_llm.complete.return_value = "queries/how-does-auth-work"
        queries_dir = tmp_path / "queries"
        _write_query_page(
            queries_dir / "how-does-auth-work.md",
            stale=True,
        )
        _write_index(tmp_path, [("queries/how-does-auth-work", "Explains auth ⚠ stale")])
        index_descriptions = {"queries/how-does-auth-work": "Explains auth ⚠ stale"}

        result = self._run("How does auth work?", tmp_path, mock_llm,
                           index_descriptions=index_descriptions)
        assert result is None

    def test_sec3_prefix_failure_returns_none(self, tmp_path):
        """LLM returns a path not starting with 'queries/' — cache miss, no file opened (AT-15)."""
        mock_llm = MagicMock()
        queries_dir = tmp_path / "queries"
        _write_query_page(queries_dir / "how-does-auth-work.md")
        index_descriptions = {"queries/how-does-auth-work": "Explains auth"}

        for bad_response in ["../sensitive/file", "summaries/auth", "secrets/passwords"]:
            mock_llm.complete.return_value = bad_response
            result = self._run("How does auth work?", tmp_path, mock_llm,
                               index_descriptions=index_descriptions)
            assert result is None, f"Expected None for response {bad_response!r}"

    def test_sec3_containment_failure_returns_none(self, tmp_path):
        """LLM returns path beginning with 'queries/' but resolving outside queries/ (AT-16)."""
        mock_llm = MagicMock()
        mock_llm.complete.return_value = "queries/../../etc/passwd"
        queries_dir = tmp_path / "queries"
        queries_dir.mkdir()
        # Put target in allowlist to force containment check (not allowlist) to catch it
        # In practice it won't be in the allowlist either, but we test containment explicitly
        index_descriptions = {"queries/../../etc/passwd": "Bad path"}

        result = self._run("How does auth work?", tmp_path, mock_llm,
                           index_descriptions=index_descriptions)
        assert result is None

    def test_sec3_allowlist_failure_returns_none(self, tmp_path):
        """LLM returns syntactically valid path not in pre-computed valid_targets (AT-17)."""
        mock_llm = MagicMock()
        mock_llm.complete.return_value = "queries/invented-page"
        queries_dir = tmp_path / "queries"
        # The actual page referenced by index_descriptions does not include invented-page
        _write_query_page(queries_dir / "how-does-auth-work.md")
        index_descriptions = {"queries/how-does-auth-work": "Explains auth"}
        # Note: "queries/invented-page" is NOT in index_descriptions → not in valid_targets

        result = self._run("How does auth work?", tmp_path, mock_llm,
                           index_descriptions=index_descriptions)
        assert result is None

    def test_cached_at_from_page_saved_at(self, tmp_path):
        """cached_at on Stage 2 hit equals the page's saved_at field."""
        mock_llm = MagicMock()
        mock_llm.complete.return_value = "queries/how-does-auth-work"
        queries_dir = tmp_path / "queries"
        _write_query_page(
            queries_dir / "how-does-auth-work.md",
            saved_at="2025-12-01 08:30:00 UTC",
        )
        index_descriptions = {"queries/how-does-auth-work": "Explains auth"}

        result = self._run("Explain auth", tmp_path, mock_llm,
                           index_descriptions=index_descriptions)
        assert result is not None
        assert result.cached_at == "2025-12-01 08:30:00 UTC"

    def test_answer_is_raw_content_stage2(self, tmp_path):
        """Stage 2 hit: answer field is verbatim raw file content (FR-QC-4)."""
        mock_llm = MagicMock()
        mock_llm.complete.return_value = "queries/how-does-auth-work"
        queries_dir = tmp_path / "queries"
        raw = _write_query_page(
            queries_dir / "how-does-auth-work.md",
            question="How does auth work?",
            answer_body="Detailed answer text here.",
        )
        index_descriptions = {"queries/how-does-auth-work": "Explains auth"}

        result = self._run("Explain auth", tmp_path, mock_llm,
                           index_descriptions=index_descriptions)
        assert result is not None
        assert result.answer == raw

    def test_parse_failure_of_stage2_file_returns_none(self, tmp_path):
        """If the validated Stage 2 file fails to parse, result is None."""
        mock_llm = MagicMock()
        mock_llm.complete.return_value = "queries/bad-file"
        queries_dir = tmp_path / "queries"
        queries_dir.mkdir()
        # Write a malformed file (no H1)
        (queries_dir / "bad-file.md").write_text(
            "no h1 line here\n## Sources\n- src/x.py.md\n", encoding="utf-8"
        )
        index_descriptions = {"queries/bad-file": "Desc"}

        result = self._run("Some question", tmp_path, mock_llm,
                           index_descriptions=index_descriptions)
        assert result is None

    def test_stale_warnings_propagated_stage2(self, tmp_path):
        """stale_warnings passed to Stage 2 are included on the returned result."""
        mock_llm = MagicMock()
        mock_llm.complete.return_value = "queries/how-does-auth-work"
        queries_dir = tmp_path / "queries"
        _write_query_page(queries_dir / "how-does-auth-work.md")
        index_descriptions = {"queries/how-does-auth-work": "Explains auth"}
        warnings = ["queries/something-stale.md"]

        result = self._run("Explain auth", tmp_path, mock_llm,
                           stale_warnings=warnings,
                           index_descriptions=index_descriptions)
        assert result is not None
        assert result.stale_warnings == ["queries/something-stale.md"]


# ---------------------------------------------------------------------------
# TestCheckQueryCache — main public entry point
# ---------------------------------------------------------------------------

class TestCheckQueryCache:
    """Tests for check_query_cache() — the public entry point."""

    def _make_config(self, codebase_path: str = "/tmp/app"):
        from codebase_wiki_builder.config import WikiConfig
        return WikiConfig(codebase_path=codebase_path)

    def _run(self, question: str, vault_root: Path, llm_client,
             index_content: str = ""):
        return check_query_cache(
            question=question,
            vault_root=vault_root,
            index_content=index_content,
            llm_client=llm_client,
            config=self._make_config(),
        )

    def test_stage1_hit_skips_stage2(self, tmp_path):
        """Stage 1 hit is returned immediately; Stage 2 (LLM) is never called."""
        queries_dir = tmp_path / "queries"
        _write_query_page(queries_dir / "how-does-auth-work.md")
        _write_index(tmp_path, STANDARD_INDEX_ROWS)
        mock_llm = MagicMock()

        result = self._run("How does auth work?", tmp_path, mock_llm)
        assert result is not None
        assert result.from_cache is True
        mock_llm.complete.assert_not_called()

    def test_stage1_miss_runs_stage2(self, tmp_path):
        """Stage 1 miss triggers Stage 2; LLM is called once."""
        queries_dir = tmp_path / "queries"
        # Create a page whose slug doesn't match the incoming question's slug
        _write_query_page(
            queries_dir / "how-does-auth-work.md",
            question="How does auth work?",
        )
        _write_index(tmp_path, STANDARD_INDEX_ROWS)
        mock_llm = MagicMock()
        mock_llm.complete.return_value = "NO_MATCH"

        # Ask a differently-phrased question — slug will differ from how-does-auth-work
        index_content = (
            "| [[queries/how-does-auth-work]] | Explains auth |\n"
        )
        result = self._run("Explain authentication mechanisms", tmp_path, mock_llm,
                           index_content=index_content)
        # Whether hit or miss, Stage 2 was called
        mock_llm.complete.assert_called_once()

    def test_both_stages_miss_returns_none(self, tmp_path):
        """Both Stage 1 and Stage 2 miss — function returns None."""
        queries_dir = tmp_path / "queries"
        queries_dir.mkdir()
        _write_index(tmp_path, [])
        mock_llm = MagicMock()
        mock_llm.complete.return_value = "NO_MATCH"

        result = self._run("How does auth work?", tmp_path, mock_llm)
        assert result is None

    def test_returns_none_on_all_error_paths(self, tmp_path):
        """check_query_cache never raises; returns None on unexpected errors."""
        mock_llm = MagicMock()

        with patch(
            "codebase_wiki_builder.query_cache.parse_existing_index",
            side_effect=RuntimeError("Unexpected internal error"),
        ):
            result = self._run("How does auth work?", tmp_path, mock_llm)
        assert result is None  # no exception raised

    def test_unexpected_exception_returns_none(self, tmp_path):
        """Any unexpected exception inside check_query_cache is swallowed (AT-11 broad)."""
        mock_llm = MagicMock()

        with patch(
            "codebase_wiki_builder.query_cache._stage1_slug_walk",
            side_effect=RuntimeError("Stage 1 exploded"),
        ):
            with patch(
                "codebase_wiki_builder.query_cache.parse_existing_index",
                return_value={},
            ):
                result = self._run("How does auth work?", tmp_path, mock_llm)
        assert result is None

    def test_from_cache_true_on_hit(self, tmp_path):
        """from_cache is True on any cache hit (Stage 1 or Stage 2)."""
        queries_dir = tmp_path / "queries"
        _write_query_page(queries_dir / "how-does-auth-work.md")
        _write_index(tmp_path, STANDARD_INDEX_ROWS)
        mock_llm = MagicMock()

        result = self._run("How does auth work?", tmp_path, mock_llm)
        assert result is not None
        assert result.from_cache is True

    def test_cached_path_set_on_hit(self, tmp_path):
        """cached_path is a vault-relative Path on a cache hit."""
        queries_dir = tmp_path / "queries"
        _write_query_page(queries_dir / "how-does-auth-work.md")
        _write_index(tmp_path, STANDARD_INDEX_ROWS)
        mock_llm = MagicMock()

        result = self._run("How does auth work?", tmp_path, mock_llm)
        assert result is not None
        assert result.cached_path == Path("queries/how-does-auth-work.md")
        # Must be relative (not absolute)
        assert not result.cached_path.is_absolute()

    def test_cached_at_set_on_hit(self, tmp_path):
        """cached_at equals the saved_at timestamp from the matched page."""
        queries_dir = tmp_path / "queries"
        _write_query_page(
            queries_dir / "how-does-auth-work.md",
            saved_at="2026-04-29 10:00:00 UTC",
        )
        _write_index(tmp_path, STANDARD_INDEX_ROWS)
        mock_llm = MagicMock()

        result = self._run("How does auth work?", tmp_path, mock_llm)
        assert result is not None
        assert result.cached_at == "2026-04-29 10:00:00 UTC"

    def test_cached_at_none_when_saved_at_missing(self, tmp_path):
        """cached_at is None when saved_at is absent — still a cache hit (OQ-3)."""
        queries_dir = tmp_path / "queries"
        queries_dir.mkdir()
        content = (
            "# How does auth work?\n\n"
            "The answer.\n\n"
            "## Sources\n- src/auth.py.md\n\n"
            "## Page Metadata\n"
            "updated_at: 2026-04-29 10:00:00 UTC\n"
        )
        (queries_dir / "how-does-auth-work.md").write_text(content, encoding="utf-8")
        _write_index(tmp_path, STANDARD_INDEX_ROWS)
        mock_llm = MagicMock()

        result = self._run("How does auth work?", tmp_path, mock_llm)
        assert result is not None
        assert result.from_cache is True
        assert result.cached_at is None  # missing saved_at → None, not a miss

    def test_stale_warnings_included_in_result(self, tmp_path):
        """stale_warnings from index_content are present on the returned QueryResult (AT-14)."""
        queries_dir = tmp_path / "queries"
        _write_query_page(queries_dir / "how-does-auth-work.md")
        # index.md has a stale annotation on another page
        index_content = (
            "| [[queries/how-does-auth-work]] | Explains auth |\n"
            "| [[queries/some-stale-page]] | Other topic ⚠ stale |\n"
        )
        (tmp_path / "index.md").write_text(
            "| File | Description |\n|------|-------------|\n" + index_content,
            encoding="utf-8",
        )
        mock_llm = MagicMock()

        result = self._run("How does auth work?", tmp_path, mock_llm,
                           index_content=index_content)
        assert result is not None
        assert any("some-stale-page" in w for w in result.stale_warnings)

    def test_no_query_pages_stage2_not_called(self, tmp_path):
        """No query pages in vault — Stage 2 LLM is not called (AT-7)."""
        queries_dir = tmp_path / "queries"
        queries_dir.mkdir()  # empty directory
        _write_index(tmp_path, [])
        mock_llm = MagicMock()

        result = self._run("How does auth work?", tmp_path, mock_llm)
        assert result is None
        mock_llm.complete.assert_not_called()

    def test_stage2_llm_error_resilience(self, tmp_path):
        """Stage 2 LLM error doesn't propagate; full pipeline would proceed (AT-12)."""
        queries_dir = tmp_path / "queries"
        # Create a page with a different slug so Stage 1 misses
        _write_query_page(
            queries_dir / "how-does-auth-work.md",
            question="How does auth work?",
        )
        index_content = "| [[queries/how-does-auth-work]] | Explains auth |\n"
        (tmp_path / "index.md").write_text(
            "| File | Description |\n|------|-------------|\n" + index_content,
            encoding="utf-8",
        )
        mock_llm = MagicMock()
        mock_llm.complete.side_effect = Exception("LLM unavailable")

        # Should return None without raising
        result = self._run("Explain authentication please", tmp_path, mock_llm,
                           index_content=index_content)
        assert result is None

    def test_stage2_hit_via_llm(self, tmp_path):
        """End-to-end Stage 2 hit: Stage 1 misses, Stage 2 finds match via LLM (AT-5)."""
        queries_dir = tmp_path / "queries"
        _write_query_page(
            queries_dir / "how-does-auth-work.md",
            question="How does auth work?",
        )
        index_content = "| [[queries/how-does-auth-work]] | Explains auth |\n"
        (tmp_path / "index.md").write_text(
            "| File | Description |\n|------|-------------|\n" + index_content,
            encoding="utf-8",
        )
        mock_llm = MagicMock()
        mock_llm.complete.return_value = "queries/how-does-auth-work"

        result = self._run("Explain authentication mechanisms", tmp_path, mock_llm,
                           index_content=index_content)
        assert result is not None
        assert result.from_cache is True
        assert result.cached_path == Path("queries/how-does-auth-work.md")
        mock_llm.complete.assert_called_once()


# ---------------------------------------------------------------------------
# TestQueryResultCacheFieldDefaults — AT-10 (from_cache default)
# ---------------------------------------------------------------------------

class TestQueryResultCacheFieldDefaults:
    """Verify the three new QueryResult fields default correctly (AT-10)."""

    def test_from_cache_defaults_false(self):
        from codebase_wiki_builder.query_engine import QueryResult
        result = QueryResult(
            answer="answer",
            sources=["src/auth.py.md"],
            one_line_summary="summary",
        )
        assert result.from_cache is False

    def test_cached_path_defaults_none(self):
        from codebase_wiki_builder.query_engine import QueryResult
        result = QueryResult(
            answer="answer",
            sources=[],
            one_line_summary="summary",
        )
        assert result.cached_path is None

    def test_cached_at_defaults_none(self):
        from codebase_wiki_builder.query_engine import QueryResult
        result = QueryResult(
            answer="answer",
            sources=[],
            one_line_summary="summary",
        )
        assert result.cached_at is None

    def test_existing_code_unaffected(self):
        """Constructing QueryResult without cache fields is backward-compatible."""
        from codebase_wiki_builder.query_engine import QueryResult
        result = QueryResult(
            answer="The answer.",
            sources=["src/x.py.md"],
            one_line_summary="One line.",
        )
        assert result.answer == "The answer."
        assert result.sources == ["src/x.py.md"]
        assert result.from_cache is False
        assert result.stale_warnings == []
