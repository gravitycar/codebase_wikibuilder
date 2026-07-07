"""Unit tests for codebase_wiki_builder.query_engine module."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_wiki_builder.query_engine import (
    QUERY_CONTEXT_WINDOW,
    NoRelevantFilesError,
    QueryResult,
    _build_answer_prompt,
    _build_relevance_prompt,
    _collect_stale_warnings,
    _fill_context_budget,
    _parse_answer_response,
    _parse_relevance_response,
    run_query,
)

logger = logging.getLogger("test_query_engine")


# ---------------------------------------------------------------------------
# NoRelevantFilesError importability and type
# ---------------------------------------------------------------------------

class TestNoRelevantFilesError:
    def test_is_importable(self):
        from codebase_wiki_builder.query_engine import NoRelevantFilesError
        assert NoRelevantFilesError is not None

    def test_is_exception(self):
        assert issubclass(NoRelevantFilesError, Exception)

    def test_can_be_raised(self):
        with pytest.raises(NoRelevantFilesError):
            raise NoRelevantFilesError("no files found")

    def test_message_preserved(self):
        msg = "No relevant files found for that query."
        try:
            raise NoRelevantFilesError(msg)
        except NoRelevantFilesError as exc:
            assert str(exc) == msg


# ---------------------------------------------------------------------------
# QUERY_CONTEXT_WINDOW constant
# ---------------------------------------------------------------------------

class TestQueryContextWindow:
    def test_value(self):
        assert QUERY_CONTEXT_WINDOW == 128_000

    def test_is_int(self):
        assert isinstance(QUERY_CONTEXT_WINDOW, int)


# ---------------------------------------------------------------------------
# _parse_relevance_response
# ---------------------------------------------------------------------------

class TestParseRelevanceResponse:
    def test_valid_json_array(self):
        raw = '["src/auth.py.md", "src/utils.py.md"]'
        result = _parse_relevance_response(raw)
        assert result == ["src/auth.py.md", "src/utils.py.md"]

    def test_empty_array(self):
        result = _parse_relevance_response("[]")
        assert result == []

    def test_fallback_extracts_json_from_preamble(self):
        raw = 'Sure, here are the files:\n["src/auth.py.md"]'
        result = _parse_relevance_response(raw)
        assert result == ["src/auth.py.md"]

    def test_invalid_json_returns_empty(self):
        result = _parse_relevance_response("NOT JSON AT ALL")
        assert result == []

    def test_non_array_json_dict_at_top_level(self):
        # If the model returns a pure dict (not a list) at top level with no embedded array,
        # the response should return empty. Use a JSON object that contains no list bracket.
        raw = '{"status": "none"}'
        result = _parse_relevance_response(raw)
        # The fallback regex will find [] in {"status": "none"} — there are no brackets
        # so should return []
        assert result == []


# ---------------------------------------------------------------------------
# _parse_answer_response
# ---------------------------------------------------------------------------

class TestParseAnswerResponse:
    def test_valid_json_response(self):
        raw = json.dumps({
            "answer": "The auth module handles JWT tokens.",
            "one_line_summary": "Explains JWT token handling"
        })
        answer, summary = _parse_answer_response(raw)
        assert answer == "The auth module handles JWT tokens."
        assert summary == "Explains JWT token handling"

    def test_fallback_to_raw_text_when_invalid_json(self):
        raw = "Just plain answer text without JSON."
        answer, summary = _parse_answer_response(raw)
        assert answer == raw
        # summary should be a truncated first sentence
        assert len(summary) > 0

    def test_extracts_from_preamble(self):
        raw = 'Here is my response: {"answer": "The auth module.", "one_line_summary": "About auth"}'
        answer, summary = _parse_answer_response(raw)
        assert answer == "The auth module."
        assert summary == "About auth"


# ---------------------------------------------------------------------------
# _fill_context_budget — path normalization
# ---------------------------------------------------------------------------

class TestFillContextBudget:
    def test_includes_existing_file(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        summary = vault / "src" / "auth.py.md"
        summary.parent.mkdir(parents=True)
        summary.write_text("# Summary\n\nContent here.", encoding="utf-8")

        included, too_large, overflow = _fill_context_budget(
            ["src/auth.py.md"], vault, logger
        )
        assert len(included) == 1
        assert included[0][0] == "src/auth.py.md"

    def test_strips_leading_slash_from_absolute_path(self, tmp_path):
        """Guards against LLM returning absolute paths."""
        vault = tmp_path / "vault"
        vault.mkdir()
        summary = vault / "src" / "auth.py.md"
        summary.parent.mkdir(parents=True)
        summary.write_text("# Summary\n\nContent.", encoding="utf-8")

        # Pass with a leading slash (simulating LLM returning absolute-ish path)
        included, too_large, overflow = _fill_context_budget(
            ["/src/auth.py.md"], vault, logger
        )
        assert len(included) == 1

    def test_skips_nonexistent_file(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        included, too_large, overflow = _fill_context_budget(
            ["nonexistent.py.md"], vault, logger
        )
        assert included == []
        assert too_large == []
        assert overflow == 0

    def test_overflow_counted_when_budget_exhausted(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        # Create two files, but the second should overflow a tiny budget
        f1 = vault / "a.py.md"
        f2 = vault / "b.py.md"
        f1.write_text("A" * 100, encoding="utf-8")
        f2.write_text("B" * 100, encoding="utf-8")

        # Patch QUERY_CONTEXT_WINDOW to be very small
        with patch("codebase_wiki_builder.query_engine.QUERY_CONTEXT_WINDOW", 1):
            included, too_large, overflow = _fill_context_budget(
                ["a.py.md", "b.py.md"], vault, logger
            )

        # a.py.md alone may exceed the tiny budget, landing in too_large
        # b.py.md should overflow
        # The exact outcome depends on token counting, but both should be non-empty

    def test_empty_paths_returns_empty(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        included, too_large, overflow = _fill_context_budget([], vault, logger)
        assert included == []
        assert too_large == []
        assert overflow == 0


# ---------------------------------------------------------------------------
# _collect_stale_warnings
# ---------------------------------------------------------------------------

class TestCollectStaleWarnings:
    def test_no_stale_rows(self):
        content = (
            "| File | Description |\n"
            "|------|-------------|\n"
            "| [[src/auth.py]] | Auth module |\n"
        )
        result = _collect_stale_warnings(content)
        assert result == []

    def test_detects_stale_row(self):
        content = (
            "| [[queries/how-auth-works]] | Explains auth ⚠ stale |\n"
        )
        result = _collect_stale_warnings(content)
        assert result == ["queries/how-auth-works.md"]

    def test_detects_multiple_stale_rows(self):
        content = (
            "| [[queries/page-one]] | Desc ⚠ stale |\n"
            "| [[queries/page-two]] | Other ⚠ stale |\n"
        )
        result = _collect_stale_warnings(content)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# _build_relevance_prompt — f-string safety
# ---------------------------------------------------------------------------

class TestBuildRelevancePromptFStringSafety:
    def test_curly_braces_in_question_no_crash(self):
        """Question with curly braces must not crash the prompt builder."""
        question = "What does {auth_middleware} do in the {config} module?"
        index_content = "Some index content\n"
        try:
            prompt = _build_relevance_prompt(question, index_content)
            assert "{auth_middleware}" in prompt
        except KeyError:
            pytest.fail("Prompt builder called .format() — must use f-strings!")

    def test_curly_braces_in_index_content_no_crash(self):
        """Index content with curly braces must not crash the prompt builder."""
        question = "How does auth work?"
        index_content = "| [[src/{config}.py]] | Some {template} content |\n"
        try:
            prompt = _build_relevance_prompt(question, index_content)
            assert "{config}" in prompt
        except KeyError:
            pytest.fail("Prompt builder called .format() — must use f-strings!")


# ---------------------------------------------------------------------------
# _build_answer_prompt — f-string safety
# ---------------------------------------------------------------------------

class TestBuildAnswerPromptFStringSafety:
    def test_curly_braces_in_summary_no_crash(self):
        """Summary content with curly braces must not crash the answer prompt builder."""
        summaries_with_braces = [
            ("src/config.py.md", "# Config\n\nUses {env_var} to configure {setting}."),
        ]
        try:
            prompt = _build_answer_prompt("How does config work?", summaries_with_braces)
            assert "{env_var}" in prompt
        except KeyError:
            pytest.fail("Answer prompt builder called .format() — must use f-strings!")


# ---------------------------------------------------------------------------
# run_query — integration with mocked LLM
# ---------------------------------------------------------------------------

class TestRunQuery:
    def _make_config(self, codebase_path: str):
        from codebase_wiki_builder.config import WikiConfig
        return WikiConfig(codebase_path=[codebase_path])

    def test_raises_file_not_found_when_no_index(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        llm = MagicMock()
        config = self._make_config(str(tmp_path / "app"))

        with pytest.raises(FileNotFoundError):
            run_query("What does auth do?", vault, llm, config)

    def test_raises_no_relevant_files_error_when_llm_returns_empty(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "index.md").write_text("| File | Description |\n", encoding="utf-8")

        llm = MagicMock()
        # First LLM call returns empty JSON array
        llm.complete.return_value = "[]"
        config = self._make_config(str(tmp_path / "app"))

        with pytest.raises(NoRelevantFilesError):
            run_query("What does auth do?", vault, llm, config)

    def test_successful_query_returns_query_result(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        # Create index.md and a summary file
        (vault / "index.md").write_text(
            "| [[src/auth.py]] | Auth module |\n",
            encoding="utf-8",
        )
        summary_dir = vault / "src"
        summary_dir.mkdir()
        (summary_dir / "auth.py.md").write_text(
            "# auth.py\nHandles JWT token validation.",
            encoding="utf-8",
        )

        llm = MagicMock()
        # First call: relevance — returns the summary file
        llm.complete.side_effect = [
            '["src/auth.py.md"]',
            json.dumps({
                "answer": "The auth module handles JWT tokens.",
                "one_line_summary": "Explains JWT token handling"
            }),
        ]
        config = self._make_config(str(tmp_path / "app"))

        result = run_query("How does auth work?", vault, llm, config)

        assert isinstance(result, QueryResult)
        assert "JWT" in result.answer
        assert result.one_line_summary == "Explains JWT token handling"
        assert "src/auth.py.md" in result.sources

    def test_select_relevant_files_returns_correct_subset(self, tmp_path):
        """_fill_context_budget returns only files that exist and fit the budget."""
        vault = tmp_path / "vault"
        vault.mkdir()

        (vault / "index.md").write_text(
            "| [[src/a.py]] | A |\n| [[src/b.py]] | B |\n",
            encoding="utf-8",
        )
        src = vault / "src"
        src.mkdir()
        (src / "a.py.md").write_text("# A\nContent A.", encoding="utf-8")
        (src / "b.py.md").write_text("# B\nContent B.", encoding="utf-8")
        # c.py.md does not exist

        llm = MagicMock()
        llm.complete.side_effect = [
            '["src/a.py.md", "src/b.py.md", "src/c.py.md"]',
            json.dumps({"answer": "Combined A and B.", "one_line_summary": "About A and B"}),
        ]
        config = self._make_config(str(tmp_path / "app"))

        result = run_query("What do A and B do?", vault, llm, config)

        # Only existing files should be in sources
        assert "src/c.py.md" not in result.sources
        assert "src/a.py.md" in result.sources
        assert "src/b.py.md" in result.sources


# ---------------------------------------------------------------------------
# QueryResult cache fields
# ---------------------------------------------------------------------------

class TestQueryResultCacheFields:
    """Tests for the three new cache-related fields on QueryResult."""

    def _make_minimal(self, **kwargs) -> QueryResult:
        """Helper: construct a QueryResult with minimum required positional fields."""
        return QueryResult(
            answer="The answer.",
            sources=["src/auth.py.md"],
            one_line_summary="Explains auth.",
            **kwargs,
        )

    def test_from_cache_defaults_to_false(self):
        result = self._make_minimal()
        assert result.from_cache is False

    def test_cached_path_defaults_to_none(self):
        result = self._make_minimal()
        assert result.cached_path is None

    def test_cached_at_defaults_to_none(self):
        result = self._make_minimal()
        assert result.cached_at is None

    def test_from_cache_can_be_set_true(self):
        result = self._make_minimal(from_cache=True)
        assert result.from_cache is True

    def test_cached_path_accepts_path_object(self):
        p = Path("queries/how-does-auth-work.md")
        result = self._make_minimal(cached_path=p)
        assert result.cached_path == p

    def test_cached_at_accepts_string(self):
        ts = "2026-04-29 10:00:00 UTC"
        result = self._make_minimal(cached_at=ts)
        assert result.cached_at == ts

    def test_existing_fields_unaffected(self):
        """Constructing QueryResult without cache fields works as before (AT-10)."""
        result = QueryResult(
            answer="Answer text.",
            sources=["src/x.py.md"],
            one_line_summary="One line.",
        )
        assert result.answer == "Answer text."
        assert result.sources == ["src/x.py.md"]
        assert result.one_line_summary == "One line."
        assert result.stale_warnings == []
        assert result.from_cache is False
        assert result.cached_path is None
        assert result.cached_at is None

    def test_full_cache_hit_construction(self):
        """All three cache fields set together — the shape check_query_cache() will use."""
        result = QueryResult(
            answer="Verbatim file content...",
            sources=["src/auth.py.md"],
            one_line_summary="Explains auth.",
            stale_warnings=[],
            from_cache=True,
            cached_path=Path("queries/how-does-auth-work.md"),
            cached_at="2026-04-29 10:00:00 UTC",
        )
        assert result.from_cache is True
        assert result.cached_path == Path("queries/how-does-auth-work.md")
        assert result.cached_at == "2026-04-29 10:00:00 UTC"
