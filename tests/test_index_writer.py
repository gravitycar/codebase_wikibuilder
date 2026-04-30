"""Unit tests for codebase_wiki_builder.index_writer module."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from codebase_wiki_builder.index_writer import (
    rebuild_index,
    _collect_query_pages,
    _collect_summary_pages,
    _extract_description,
    _overview_description,
    _parse_existing_index,
    _write_index,
)

logger = logging.getLogger("test_index_writer")


# ---------------------------------------------------------------------------
# rebuild_index — round-trip correctness
# ---------------------------------------------------------------------------

class TestRebuildIndex:
    def test_creates_index_md(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "main.py.md").write_text("# main.py\n\nEntry point.", encoding="utf-8")

        rebuild_index(vault, logger)

        assert (vault / "index.md").exists()

    def test_index_contains_summary_file(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "main.py.md").write_text("# main.py\n\nEntry point.", encoding="utf-8")

        rebuild_index(vault, logger)

        content = (vault / "index.md").read_text(encoding="utf-8")
        assert "[[main.py]]" in content

    def test_index_has_table_header(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        rebuild_index(vault, logger)

        content = (vault / "index.md").read_text(encoding="utf-8")
        assert "| File | Description |" in content
        assert "|------|-------------|" in content

    def test_query_pages_included(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        queries = vault / "queries"
        queries.mkdir()
        (queries / "how-auth-works.md").write_text(
            "# How does auth work?\n\nAnswer.", encoding="utf-8"
        )

        rebuild_index(vault, logger)

        content = (vault / "index.md").read_text(encoding="utf-8")
        assert "[[queries/how-auth-works]]" in content

    def test_preserves_query_description_from_old_index(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        queries = vault / "queries"
        queries.mkdir()
        (queries / "how-auth-works.md").write_text(
            "# How does auth work?\n\nThis explains JWT.", encoding="utf-8"
        )

        # Pre-create index with a custom description
        existing_index = vault / "index.md"
        existing_index.write_text(
            "| File | Description |\n"
            "|------|-------------|\n"
            "| [[queries/how-auth-works]] | Custom description ⚠ stale |\n",
            encoding="utf-8",
        )

        rebuild_index(vault, logger)

        content = existing_index.read_text(encoding="utf-8")
        # Old description should be preserved (including stale annotation)
        assert "Custom description ⚠ stale" in content

    def test_special_files_excluded(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        for special in ("index.md", "log.md", "lint-report.md"):
            (vault / special).write_text("content", encoding="utf-8")

        rebuild_index(vault, logger)

        content = (vault / "index.md").read_text(encoding="utf-8")
        # These special files should not appear as entries
        assert "[[log]]" not in content
        assert "[[lint-report]]" not in content

    def test_overview_files_included(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "overview.md").write_text("# Overview\n\nApp summary.", encoding="utf-8")

        rebuild_index(vault, logger)

        content = (vault / "index.md").read_text(encoding="utf-8")
        assert "[[overview]]" in content


# ---------------------------------------------------------------------------
# _parse_existing_index
# ---------------------------------------------------------------------------

class TestParseExistingIndex:
    def test_returns_empty_when_no_index(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        result = _parse_existing_index(vault)
        assert result == {}

    def test_parses_description(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "index.md").write_text(
            "| File | Description |\n"
            "|------|-------------|\n"
            "| [[src/auth.py]] | Handles JWT |\n",
            encoding="utf-8",
        )
        result = _parse_existing_index(vault)
        assert result.get("src/auth.py") == "Handles JWT"

    def test_parses_stale_annotation(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "index.md").write_text(
            "| [[queries/how-auth-works]] | Explains auth ⚠ stale |\n",
            encoding="utf-8",
        )
        result = _parse_existing_index(vault)
        assert "⚠ stale" in result.get("queries/how-auth-works", "")


# ---------------------------------------------------------------------------
# _extract_description
# ---------------------------------------------------------------------------

class TestExtractDescription:
    def test_extracts_first_non_blank_non_heading_line(self, tmp_path):
        f = tmp_path / "page.md"
        f.write_text("# Title\n\nThis is the description.\n", encoding="utf-8")
        assert _extract_description(f) == "This is the description."

    def test_skips_h1(self, tmp_path):
        f = tmp_path / "page.md"
        f.write_text("# Title\n\nDescription line.\n", encoding="utf-8")
        result = _extract_description(f)
        assert result == "Description line."
        assert "Title" not in result

    def test_falls_back_when_no_description(self, tmp_path):
        f = tmp_path / "page.md"
        f.write_text("# Title\n\n## Section\n", encoding="utf-8")
        assert _extract_description(f) == "(no description)"

    def test_truncates_to_120_chars(self, tmp_path):
        f = tmp_path / "page.md"
        long_line = "x" * 200
        f.write_text(f"# Title\n\n{long_line}\n", encoding="utf-8")
        result = _extract_description(f)
        assert len(result) <= 120

    def test_returns_no_description_for_missing_file(self, tmp_path):
        f = tmp_path / "nonexistent.md"
        assert _extract_description(f) == "(no description)"


# ---------------------------------------------------------------------------
# _overview_description
# ---------------------------------------------------------------------------

class TestOverviewDescription:
    def test_root_overview(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        overview = vault / "overview.md"
        result = _overview_description(overview, vault)
        assert result == "Top-level application overview"

    def test_subdir_overview(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "src" / "auth").mkdir(parents=True)
        overview = vault / "src" / "auth" / "overview.md"
        result = _overview_description(overview, vault)
        assert result == "Directory overview: src/auth/"


# ---------------------------------------------------------------------------
# _collect_summary_pages
# ---------------------------------------------------------------------------

class TestCollectSummaryPages:
    def test_excludes_queries_dir(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        queries = vault / "queries"
        queries.mkdir()
        (queries / "q.md").write_text("content", encoding="utf-8")
        (vault / "code.py.md").write_text("content", encoding="utf-8")

        result = _collect_summary_pages(vault)
        names = [p.name for p in result]
        assert "q.md" not in names
        assert "code.py.md" in names

    def test_excludes_overview_files(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "overview.md").write_text("overview", encoding="utf-8")
        (vault / "main.py.md").write_text("summary", encoding="utf-8")

        result = _collect_summary_pages(vault)
        names = [p.name for p in result]
        assert "overview.md" not in names
        assert "main.py.md" in names


# ---------------------------------------------------------------------------
# _collect_query_pages
# ---------------------------------------------------------------------------

class TestCollectQueryPages:
    def test_returns_empty_when_no_queries_dir(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        result = _collect_query_pages(vault)
        assert result == []

    def test_returns_md_files_in_queries(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        queries = vault / "queries"
        queries.mkdir()
        (queries / "q1.md").write_text("# Q1", encoding="utf-8")
        (queries / "q2.md").write_text("# Q2", encoding="utf-8")

        result = _collect_query_pages(vault)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# _write_index — pipe escaping
# ---------------------------------------------------------------------------

class TestWriteIndex:
    def test_escapes_pipe_in_description(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        rows = [("[[src/a.py]]", "Left | Right")]
        _write_index(vault, rows, logger)
        content = (vault / "index.md").read_text(encoding="utf-8")
        assert "Left \\| Right" in content
