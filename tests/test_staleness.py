"""Unit tests for codebase_wiki_builder.staleness module."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_wiki_builder.scanner import ChangeSet
from codebase_wiki_builder.staleness import (
    StalenessResult,
    has_stale_banner,
    _insert_stale_banner,
    _annotate_index_row,
    _parse_sources_section,
    detect_stale_queries,
)

logger = logging.getLogger("test_staleness")


def make_log_fn():
    """Return a simple log accumulator."""
    entries = []
    def log_fn(entry: str) -> None:
        entries.append(entry)
    log_fn.entries = entries
    return log_fn


# ---------------------------------------------------------------------------
# _parse_sources_section
# ---------------------------------------------------------------------------

class TestParseSourcesSection:
    def test_returns_none_when_no_sources_section(self):
        content = "# My Query\n\nSome answer text.\n"
        assert _parse_sources_section(content) is None

    def test_returns_empty_list_for_empty_sources(self):
        content = "# My Query\n\n## Sources\n\n(no items)\n"
        result = _parse_sources_section(content)
        assert result == []

    def test_parses_single_source(self):
        content = "# My Query\n\nAnswer.\n\n## Sources\n- src/auth/login.py.md\n"
        result = _parse_sources_section(content)
        assert result == ["src/auth/login.py.md"]

    def test_parses_multiple_sources(self):
        content = (
            "# My Query\n\nAnswer.\n\n## Sources\n"
            "- src/auth/login.py.md\n"
            "- src/auth/utils.py.md\n"
            "- config.py.md\n"
        )
        result = _parse_sources_section(content)
        assert result == ["src/auth/login.py.md", "src/auth/utils.py.md", "config.py.md"]

    def test_stops_at_next_heading(self):
        content = (
            "# My Query\n\nAnswer.\n\n## Sources\n"
            "- src/auth.py.md\n"
            "\n## Page Metadata\n"
            "saved_at: 2026-01-01\n"
        )
        result = _parse_sources_section(content)
        assert result == ["src/auth.py.md"]

    def test_strips_annotation_from_source_path(self):
        # _SOURCE_ITEM_RE captures \S+ which stops at whitespace
        content = "## Sources\n- src/big.py.md (too large to include)\n"
        result = _parse_sources_section(content)
        assert result == ["src/big.py.md"]


# ---------------------------------------------------------------------------
# has_stale_banner
# ---------------------------------------------------------------------------

class TestHasStaleBanner:
    def test_no_banner(self):
        content = "# Query\n\nAnswer\n"
        assert has_stale_banner(content) is False

    def test_detects_stale_banner(self):
        content = "# Query\n\n> [!warning] Stale Content\n> Sources changed\n\nAnswer\n"
        assert has_stale_banner(content) is True

    def test_case_sensitive(self):
        # The banner check is for the exact pattern
        content = "# Query\n\n> [!warning] stale content\n"
        # lowercase 'stale content' — regex is case-sensitive on 'Stale Content'
        assert has_stale_banner(content) is False


# ---------------------------------------------------------------------------
# _insert_stale_banner
# ---------------------------------------------------------------------------

class TestInsertStaleBanner:
    def test_inserts_banner_after_h1(self, tmp_path):
        query_page = tmp_path / "query.md"
        query_page.write_text(
            "# How does auth work?\n\nAnswer text.\n\n## Sources\n- src/auth.py.md\n",
            encoding="utf-8",
        )

        _insert_stale_banner(query_page, ["src/auth.py.md"], logger)

        content = query_page.read_text(encoding="utf-8")
        assert "> [!warning] Stale Content" in content
        assert "src/auth.py.md" in content

    def test_h1_remains_first_line(self, tmp_path):
        query_page = tmp_path / "query.md"
        query_page.write_text(
            "# My Question\n\n## Sources\n- src/a.py.md\n",
            encoding="utf-8",
        )
        _insert_stale_banner(query_page, ["src/a.py.md"], logger)

        lines = query_page.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "# My Question"

    def test_skips_blank_lines_after_h1(self, tmp_path):
        query_page = tmp_path / "query.md"
        query_page.write_text(
            "# My Question\n\n\nAnswer text here.\n",
            encoding="utf-8",
        )
        _insert_stale_banner(query_page, ["src/a.py.md"], logger)

        content = query_page.read_text(encoding="utf-8")
        assert "> [!warning] Stale Content" in content
        # H1 must still be first
        assert content.startswith("# My Question")


# ---------------------------------------------------------------------------
# _annotate_index_row
# ---------------------------------------------------------------------------

class TestAnnotateIndexRow:
    def test_annotates_matching_row(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        index = vault / "index.md"
        index.write_text(
            "| File | Description |\n"
            "|------|-------------|\n"
            "| [[queries/how-auth-works]] | Explains auth |\n",
            encoding="utf-8",
        )
        query_page = vault / "queries" / "how-auth-works.md"

        _annotate_index_row(vault, query_page, logger)

        content = index.read_text(encoding="utf-8")
        assert "⚠ stale" in content

    def test_does_not_double_annotate(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        index = vault / "index.md"
        index.write_text(
            "| [[queries/how-auth-works]] | Explains auth ⚠ stale |\n",
            encoding="utf-8",
        )
        query_page = vault / "queries" / "how-auth-works.md"

        _annotate_index_row(vault, query_page, logger)

        content = index.read_text(encoding="utf-8")
        # Should only appear once
        assert content.count("⚠ stale") == 1

    def test_no_op_when_index_missing(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        # No index.md created
        query_page = vault / "queries" / "some-query.md"
        # Should not raise
        _annotate_index_row(vault, query_page, logger)


# ---------------------------------------------------------------------------
# detect_stale_queries — integration
# ---------------------------------------------------------------------------

class TestDetectStaleQueries:
    def test_no_queries_dir_returns_empty_result(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        codebase = tmp_path / "app"
        codebase.mkdir()

        change_set = ChangeSet()
        log_fn = make_log_fn()

        result = detect_stale_queries(change_set, vault, codebase, log_fn, logger)

        assert result.flagged_pages == []
        assert result.clean_pages == []
        assert result.malformed_sources_pages == []

    def test_clean_query_page_not_flagged(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        codebase = tmp_path / "app"
        codebase.mkdir()
        queries_dir = vault / "queries"
        queries_dir.mkdir()

        query_page = queries_dir / "how-auth-works.md"
        query_page.write_text(
            "# How does auth work?\n\nAnswer.\n\n## Sources\n- src/auth.py.md\n",
            encoding="utf-8",
        )

        # No changed files in the change set
        change_set = ChangeSet()
        log_fn = make_log_fn()

        result = detect_stale_queries(change_set, vault, codebase, log_fn, logger)

        assert result.clean_pages == [query_page]
        assert result.flagged_pages == []

    def test_stale_query_page_is_flagged(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        codebase = tmp_path / "app"
        codebase.mkdir()
        queries_dir = vault / "queries"
        queries_dir.mkdir()

        # Create the query page referencing src/auth.py.md
        query_page = queries_dir / "how-auth-works.md"
        query_page.write_text(
            "# How does auth work?\n\nAnswer.\n\n## Sources\n- src/auth.py.md\n",
            encoding="utf-8",
        )

        # Create index.md with a row for this query
        (vault / "index.md").write_text(
            "| [[queries/how-auth-works]] | Explains auth |\n",
            encoding="utf-8",
        )

        # Change set has auth.py as modified → its vault path is src/auth.py.md
        source_file = codebase / "src" / "auth.py"
        change_set = ChangeSet(modified_files=[source_file])
        log_fn = make_log_fn()

        result = detect_stale_queries(change_set, vault, codebase, log_fn, logger)

        assert result.flagged_pages == [query_page]
        # Banner should have been inserted
        content = query_page.read_text(encoding="utf-8")
        assert "> [!warning] Stale Content" in content

    def test_malformed_sources_page_reported(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        codebase = tmp_path / "app"
        codebase.mkdir()
        queries_dir = vault / "queries"
        queries_dir.mkdir()

        # Create a query page with NO ## Sources section
        bad_page = queries_dir / "no-sources.md"
        bad_page.write_text(
            "# Query without sources\n\nJust some text.\n",
            encoding="utf-8",
        )

        change_set = ChangeSet()
        log_fn = make_log_fn()

        result = detect_stale_queries(change_set, vault, codebase, log_fn, logger)

        assert result.malformed_sources_pages == [bad_page]

    def test_already_stale_page_not_re_flagged(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        codebase = tmp_path / "app"
        codebase.mkdir()
        queries_dir = vault / "queries"
        queries_dir.mkdir()

        # Page already has a stale banner
        query_page = queries_dir / "already-stale.md"
        query_page.write_text(
            "# Already Stale\n\n"
            "> [!warning] Stale Content\n"
            "> Source changed\n\n"
            "Answer.\n\n## Sources\n- src/a.py.md\n",
            encoding="utf-8",
        )

        change_set = ChangeSet()
        log_fn = make_log_fn()

        result = detect_stale_queries(change_set, vault, codebase, log_fn, logger)

        assert result.already_stale_pages == [query_page]
        assert result.flagged_pages == []
