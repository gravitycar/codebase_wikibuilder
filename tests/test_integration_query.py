"""Integration tests for the full query workflow.

Tests end-to-end flows:
  - run_query() with mocked LLM returns a QueryResult
  - save_query_page() writes the page to queries/ and updates index.md
  - NoRelevantFilesError raised when LLM returns no relevant files

Uses real filesystem (tmp_path). Mocks only LLMClient.complete().
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_wiki_builder.config import WikiConfig
from codebase_wiki_builder.query_engine import run_query, NoRelevantFilesError, QueryResult
from codebase_wiki_builder.query_persistence import save_query_page, read_query_page

logger = logging.getLogger("test_integration_query")


def make_config(codebase_path: Path) -> WikiConfig:
    return WikiConfig(
        codebase_path=str(codebase_path),
        llm_provider="anthropic",
        llm_model="claude-sonnet-4-6",
        file_size_threshold=100_000,
        inter_request_delay=0.0,
    )


def make_vault_with_summaries(tmp_path: Path) -> tuple[Path, Path]:
    """
    Create a vault with:
      - index.md listing two summary files
      - src/auth.py.md and src/utils.py.md

    Returns (vault_root, codebase_root).
    """
    vault = tmp_path / "vault"
    codebase = tmp_path / "codebase"
    vault.mkdir()
    codebase.mkdir()

    src_dir = vault / "src"
    src_dir.mkdir()

    (src_dir / "auth.py.md").write_text(
        "# src/auth.py\n\nHandles JWT authentication.\n\n"
        "## References\n\n"
        "<!-- md5: aabbccdd00112233aabbccdd00112233 -->",
        encoding="utf-8",
    )
    (src_dir / "utils.py.md").write_text(
        "# src/utils.py\n\nUtility functions.\n\n"
        "## References\n\n"
        "<!-- md5: 11223344aabbccdd11223344aabbccdd -->",
        encoding="utf-8",
    )

    (vault / "index.md").write_text(
        "| File | Description |\n"
        "|------|-------------|\n"
        "| [[src/auth.py]] | Auth module |\n"
        "| [[src/utils.py]] | Utils |\n",
        encoding="utf-8",
    )

    return vault, codebase


def log_fn_noop(entry: str) -> None:
    pass


# ---------------------------------------------------------------------------
# run_query: happy path
# ---------------------------------------------------------------------------

class TestRunQueryHappyPath:
    def test_run_query_returns_result_with_answer(self, tmp_path, monkeypatch):
        """run_query() returns a QueryResult with non-empty answer."""
        vault, codebase = make_vault_with_summaries(tmp_path)
        config = make_config(codebase)

        # Patch token counting to keep tests fast
        monkeypatch.setattr(
            "codebase_wiki_builder.query_engine._count_tokens",
            lambda text: 100,
        )

        mock_llm = MagicMock()
        mock_llm.complete.side_effect = [
            '["src/auth.py.md", "src/utils.py.md"]',
            json.dumps({
                "answer": "Auth uses JWT tokens.",
                "one_line_summary": "Explains JWT authentication",
            }),
        ]

        result = run_query("How does auth work?", vault, mock_llm, config)

        assert isinstance(result, QueryResult)
        assert "JWT" in result.answer
        assert result.one_line_summary == "Explains JWT authentication"
        assert "## Sources" in result.answer
        assert mock_llm.complete.call_count == 2

    def test_run_query_sources_list_populated(self, tmp_path, monkeypatch):
        """run_query() populates the sources list from LLM-selected files."""
        vault, codebase = make_vault_with_summaries(tmp_path)
        config = make_config(codebase)

        monkeypatch.setattr(
            "codebase_wiki_builder.query_engine._count_tokens",
            lambda text: 100,
        )

        mock_llm = MagicMock()
        mock_llm.complete.side_effect = [
            '["src/auth.py.md"]',
            json.dumps({
                "answer": "The answer.",
                "one_line_summary": "Describes auth.",
            }),
        ]

        result = run_query("What is auth?", vault, mock_llm, config)
        assert "src/auth.py.md" in result.sources

    def test_run_query_stale_warnings_detected(self, tmp_path, monkeypatch):
        """run_query() collects stale warnings from index.md."""
        vault, codebase = make_vault_with_summaries(tmp_path)
        config = make_config(codebase)

        # Mark one row as stale
        index_content = (vault / "index.md").read_text(encoding="utf-8")
        index_content = index_content.replace(
            "| [[src/auth.py]] | Auth module |",
            "| [[queries/how-auth-works]] | How auth works ⚠ stale |",
        )
        (vault / "index.md").write_text(index_content, encoding="utf-8")

        # Create the queries dir and file for completeness
        (vault / "queries").mkdir()
        (vault / "queries" / "how-auth-works.md").write_text(
            "# How auth works?\n\nOld answer.\n\n## Sources\n- src/auth.py.md\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(
            "codebase_wiki_builder.query_engine._count_tokens",
            lambda text: 100,
        )

        mock_llm = MagicMock()
        mock_llm.complete.side_effect = [
            # Stage 2 LLM pre-check consumes one response (queries dir has a file)
            "NO_MATCH",
            # Full pipeline: relevance call + answer call
            '["src/utils.py.md"]',
            json.dumps({
                "answer": "Utilities answer.",
                "one_line_summary": "About utilities.",
            }),
        ]

        result = run_query("What do utils do?", vault, mock_llm, config)
        assert "queries/how-auth-works.md" in result.stale_warnings

    def test_run_query_raises_when_no_index(self, tmp_path):
        """run_query() raises FileNotFoundError when no index.md exists."""
        vault = tmp_path / "vault"
        vault.mkdir()
        codebase = tmp_path / "codebase"
        codebase.mkdir()
        config = make_config(codebase)

        mock_llm = MagicMock()
        with pytest.raises(FileNotFoundError, match="no summaries"):
            run_query("What is this?", vault, mock_llm, config)


# ---------------------------------------------------------------------------
# run_query: NoRelevantFilesError
# ---------------------------------------------------------------------------

class TestRunQueryNoRelevantFiles:
    def test_empty_array_raises_no_relevant_files_error(self, tmp_path, monkeypatch):
        """When LLM returns empty JSON array, NoRelevantFilesError is raised."""
        vault, codebase = make_vault_with_summaries(tmp_path)
        config = make_config(codebase)

        mock_llm = MagicMock()
        mock_llm.complete.return_value = "[]"

        with pytest.raises(NoRelevantFilesError):
            run_query("Completely irrelevant question", vault, mock_llm, config)

    def test_unparseable_response_raises_no_relevant_files_error(self, tmp_path):
        """When LLM returns non-JSON, NoRelevantFilesError is raised."""
        vault, codebase = make_vault_with_summaries(tmp_path)
        config = make_config(codebase)

        mock_llm = MagicMock()
        mock_llm.complete.return_value = "Sorry, I cannot find any relevant files."

        with pytest.raises(NoRelevantFilesError):
            run_query("Some question", vault, mock_llm, config)


# ---------------------------------------------------------------------------
# save_query_page: integration
# ---------------------------------------------------------------------------

class TestSaveQueryPage:
    def test_save_query_page_creates_file(self, tmp_path, monkeypatch):
        """save_query_page() creates a file in queries/ with correct content."""
        vault, codebase = make_vault_with_summaries(tmp_path)
        config = make_config(codebase)

        monkeypatch.setattr(
            "codebase_wiki_builder.query_engine._count_tokens",
            lambda text: 100,
        )

        mock_llm = MagicMock()
        mock_llm.complete.side_effect = [
            '["src/auth.py.md"]',
            json.dumps({
                "answer": "Auth uses JWT.\n\n## Sources\n- src/auth.py.md",
                "one_line_summary": "Explains JWT auth",
            }),
        ]

        result = run_query("How does auth work?", vault, mock_llm, config)
        log_entries: list[str] = []
        saved_path = save_query_page("How does auth work?", result, vault, log_entries.append)

        # Verify file exists
        assert saved_path.exists()
        assert saved_path.parent.name == "queries"
        assert saved_path.stem == "how-does-auth-work"

        # Verify content structure
        content = saved_path.read_text(encoding="utf-8")
        lines = content.splitlines()

        # H1 is the first line
        assert lines[0] == "# How does auth work?"
        # Answer body present
        assert "JWT" in content
        # Sources section present
        assert "## Sources" in content
        # Page Metadata footer
        assert "## Page Metadata" in content
        assert "saved_at:" in content
        assert "updated_at:" in content

    def test_save_query_page_updates_index_md(self, tmp_path, monkeypatch):
        """save_query_page() adds a row to index.md."""
        vault, codebase = make_vault_with_summaries(tmp_path)
        config = make_config(codebase)

        monkeypatch.setattr(
            "codebase_wiki_builder.query_engine._count_tokens",
            lambda text: 100,
        )

        mock_llm = MagicMock()
        mock_llm.complete.side_effect = [
            '["src/utils.py.md"]',
            json.dumps({
                "answer": "Utils do helpers.\n\n## Sources\n- src/utils.py.md",
                "one_line_summary": "Explains utility functions",
            }),
        ]

        result = run_query("What do utils do?", vault, mock_llm, config)
        save_query_page("What do utils do?", result, vault, log_fn_noop)

        index_content = (vault / "index.md").read_text(encoding="utf-8")
        assert "[[queries/what-do-utils-do]]" in index_content
        assert "Explains utility functions" in index_content

    def test_save_query_page_writes_log_entry(self, tmp_path, monkeypatch):
        """save_query_page() calls log_fn with a query-saved entry."""
        vault, codebase = make_vault_with_summaries(tmp_path)
        config = make_config(codebase)

        monkeypatch.setattr(
            "codebase_wiki_builder.query_engine._count_tokens",
            lambda text: 100,
        )

        mock_llm = MagicMock()
        mock_llm.complete.side_effect = [
            '["src/auth.py.md"]',
            json.dumps({
                "answer": "Answer.\n\n## Sources\n- src/auth.py.md",
                "one_line_summary": "One line",
            }),
        ]

        result = run_query("What is auth?", vault, mock_llm, config)
        log_entries: list[str] = []
        save_query_page("What is auth?", result, vault, log_entries.append)

        assert len(log_entries) == 1
        assert "query-saved" in log_entries[0]
        assert "What is auth?" in log_entries[0]
        assert "queries/" in log_entries[0]

    def test_save_query_page_no_overwrite(self, tmp_path, monkeypatch):
        """save_query_page() never overwrites an existing query page."""
        vault, codebase = make_vault_with_summaries(tmp_path)
        config = make_config(codebase)

        monkeypatch.setattr(
            "codebase_wiki_builder.query_engine._count_tokens",
            lambda text: 100,
        )

        # Pre-create the expected query page
        queries_dir = vault / "queries"
        queries_dir.mkdir(exist_ok=True)
        existing = queries_dir / "how-does-auth-work.md"
        existing.write_text("ORIGINAL CONTENT", encoding="utf-8")

        result = QueryResult(
            answer="New answer.\n\n## Sources\n- src/auth.py.md",
            sources=["src/auth.py.md"],
            one_line_summary="Explains auth",
            stale_warnings=[],
        )
        saved_path = save_query_page("How does auth work?", result, vault, log_fn_noop)

        # Must save to a different file
        assert saved_path != existing
        assert saved_path.stem.startswith("how-does-auth-work-")

        # Original file not overwritten
        assert existing.read_text(encoding="utf-8") == "ORIGINAL CONTENT"

    def test_save_then_read_roundtrip(self, tmp_path, monkeypatch):
        """save_query_page → read_query_page round-trip preserves key fields."""
        vault, codebase = make_vault_with_summaries(tmp_path)
        config = make_config(codebase)

        monkeypatch.setattr(
            "codebase_wiki_builder.query_engine._count_tokens",
            lambda text: 100,
        )

        mock_llm = MagicMock()
        mock_llm.complete.side_effect = [
            '["src/auth.py.md"]',
            json.dumps({
                "answer": "Flask is used.\n\n## Sources\n- src/auth.py.md",
                "one_line_summary": "Describes Flask usage",
            }),
        ]

        question = "What patterns does this codebase use?"
        result = run_query(question, vault, mock_llm, config)
        saved_path = save_query_page(question, result, vault, log_fn_noop)

        # Read back the page
        page = read_query_page(saved_path)

        assert page.question == question
        assert "Flask" in page.answer_body or "Flask" in page.raw_content
        assert page.saved_at != ""
        assert page.updated_at != ""
        assert page.saved_at == page.updated_at  # both set at creation time
        assert page.sources  # at least one source
