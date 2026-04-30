"""Unit tests for codebase_wiki_builder.lint_dedup module."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_wiki_builder.lint_dedup import (
    LintDedupResult,
    _build_detection_prompt,
    _build_merge_prompt,
    _collect_query_entries,
    _extract_first_prose,
    _parse_timestamp,
    _QueryPageEntry,
    deduplicate_query_pages,
)

logger = logging.getLogger("test_lint_dedup")


def make_log_fn():
    entries = []
    def log_fn(entry: str) -> None:
        entries.append(entry)
    log_fn.entries = entries
    return log_fn


# ---------------------------------------------------------------------------
# _parse_timestamp
# ---------------------------------------------------------------------------

class TestParseTimestamp:
    def test_valid_timestamp(self):
        ts = _parse_timestamp("2026-04-29 10:00:00 UTC")
        assert ts is not None
        assert ts.year == 2026
        assert ts.month == 4
        assert ts.day == 29

    def test_invalid_timestamp_returns_none(self):
        assert _parse_timestamp("not a timestamp") is None

    def test_empty_string_returns_none(self):
        assert _parse_timestamp("") is None


# ---------------------------------------------------------------------------
# _collect_query_entries
# ---------------------------------------------------------------------------

class TestCollectQueryEntries:
    def test_returns_empty_when_no_index(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        result = _collect_query_entries(vault)
        assert result == []

    def test_returns_query_entries_only(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "index.md").write_text(
            "| File | Description |\n"
            "|------|-------------|\n"
            "| [[src/auth.py]] | Auth module |\n"
            "| [[queries/how-auth-works]] | Explains auth |\n",
            encoding="utf-8",
        )
        result = _collect_query_entries(vault)
        assert len(result) == 1
        assert result[0].wikilink_target == "queries/how-auth-works"
        assert result[0].description == "Explains auth"

    def test_strips_stale_annotation(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "index.md").write_text(
            "| [[queries/auth-query]] | Explains auth ⚠ stale |\n",
            encoding="utf-8",
        )
        result = _collect_query_entries(vault)
        assert result[0].description == "Explains auth"

    def test_multiple_query_entries(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "index.md").write_text(
            "| [[queries/q1]] | First query |\n"
            "| [[queries/q2]] | Second query |\n",
            encoding="utf-8",
        )
        result = _collect_query_entries(vault)
        assert len(result) == 2
        assert result[0].row_index == 0
        assert result[1].row_index == 1


# ---------------------------------------------------------------------------
# _extract_first_prose
# ---------------------------------------------------------------------------

class TestExtractFirstProse:
    def test_returns_first_prose_line(self):
        content = "# Title\n\nThis is the first prose line.\n\nMore content."
        assert _extract_first_prose(content) == "This is the first prose line."

    def test_skips_h1(self):
        content = "# My Title\n\nProse here."
        result = _extract_first_prose(content)
        assert "My Title" not in result
        assert result == "Prose here."

    def test_returns_fallback_for_empty_content(self):
        content = "# Title\n\n## Section\n"
        assert _extract_first_prose(content) == "(merged query page)"

    def test_truncates_to_120_chars(self):
        content = "# Title\n\n" + "x" * 200
        result = _extract_first_prose(content)
        assert len(result) <= 120


# ---------------------------------------------------------------------------
# _build_detection_prompt — f-string safety
# ---------------------------------------------------------------------------

class TestBuildDetectionPromptFStringSafety:
    def test_curly_braces_in_page_list_no_crash(self):
        """Page list with curly braces must not crash the prompt builder."""
        page_list = "queries/how-auth-works.md: Uses {token} for {auth}"
        try:
            prompt = _build_detection_prompt(page_list)
            assert "{token}" in prompt
        except KeyError:
            pytest.fail("Detection prompt builder called .format() on untrusted content!")

    def test_empty_page_list(self):
        prompt = _build_detection_prompt("")
        assert "JSON array" in prompt


# ---------------------------------------------------------------------------
# _build_merge_prompt — f-string safety
# ---------------------------------------------------------------------------

class TestBuildMergePromptFStringSafety:
    def test_curly_braces_in_pages_content_no_crash(self):
        """Pages content with curly braces must not crash the merge prompt builder."""
        pages_content = "--- SURVIVING PAGE ---\nContent with {curly} braces {here}."
        try:
            prompt = _build_merge_prompt(pages_content)
            assert "{curly}" in prompt
        except KeyError:
            pytest.fail("Merge prompt builder called .format() on untrusted content!")


# ---------------------------------------------------------------------------
# deduplicate_query_pages — fewer than 2 pages
# ---------------------------------------------------------------------------

class TestDeduplicateQueryPagesFewerThanTwo:
    def test_returns_empty_when_no_pages(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "index.md").write_text("| File | Description |\n", encoding="utf-8")

        llm = MagicMock()
        log_fn = make_log_fn()

        result = deduplicate_query_pages(vault, llm, log_fn)

        assert result.merged_groups == []
        assert result.skipped_pages == []
        # LLM should not be called when there's fewer than 2 pages
        llm.complete.assert_not_called()

    def test_returns_empty_when_one_page(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        queries = vault / "queries"
        queries.mkdir()
        (queries / "q1.md").write_text("# Q1\nAnswer.\n\n## Sources\n- a.py.md\n", encoding="utf-8")
        (vault / "index.md").write_text(
            "| [[queries/q1]] | First query |\n",
            encoding="utf-8",
        )

        llm = MagicMock()
        log_fn = make_log_fn()

        result = deduplicate_query_pages(vault, llm, log_fn)

        assert result.merged_groups == []
        llm.complete.assert_not_called()


# ---------------------------------------------------------------------------
# deduplicate_query_pages — no duplicates detected
# ---------------------------------------------------------------------------

class TestDeduplicateQueryPagesNoDuplicates:
    def test_returns_empty_when_no_duplicates_detected(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        queries = vault / "queries"
        queries.mkdir()

        for i, name in enumerate(["q1", "q2"]):
            p = queries / f"{name}.md"
            p.write_text(
                f"# Question {i}\n\nAnswer.\n\n## Sources\n- src/{name}.py.md\n"
                f"\n## Page Metadata\nsaved_at: 2026-01-01 00:00:00 UTC\nupdated_at: 2026-01-01 00:00:00 UTC\n",
                encoding="utf-8",
            )

        (vault / "index.md").write_text(
            "| [[queries/q1]] | First query |\n"
            "| [[queries/q2]] | Second query |\n",
            encoding="utf-8",
        )

        llm = MagicMock()
        # LLM returns empty array — no duplicates
        llm.complete.return_value = "[]"
        log_fn = make_log_fn()

        result = deduplicate_query_pages(vault, llm, log_fn)

        assert result.merged_groups == []


# ---------------------------------------------------------------------------
# deduplicate_query_pages — dedup logic
# ---------------------------------------------------------------------------

class TestDeduplicateQueryPagesMerge:
    def test_merges_duplicate_pages(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        queries = vault / "queries"
        queries.mkdir()

        # Create two query pages that the LLM will flag as duplicates
        q1 = queries / "q1.md"
        q1.write_text(
            "# How does auth work?\n\nAnswer about auth.\n\n"
            "## Sources\n- src/auth.py.md\n\n"
            "## Page Metadata\nsaved_at: 2026-01-01 00:00:00 UTC\nupdated_at: 2026-01-01 00:00:00 UTC\n",
            encoding="utf-8",
        )
        q2 = queries / "q2.md"
        q2.write_text(
            "# Explain authentication\n\nAnswer about authentication.\n\n"
            "## Sources\n- src/auth.py.md\n\n"
            "## Page Metadata\nsaved_at: 2026-01-02 00:00:00 UTC\nupdated_at: 2026-01-02 00:00:00 UTC\n",
            encoding="utf-8",
        )

        (vault / "index.md").write_text(
            "| [[queries/q1]] | How does auth work? |\n"
            "| [[queries/q2]] | Explain authentication |\n",
            encoding="utf-8",
        )

        llm = MagicMock()
        # Detection pass returns the two as duplicates
        llm.complete.side_effect = [
            # First call: detection pass
            json.dumps([["queries/q1.md", "queries/q2.md"]]),
            # Second call: merge pass
            "# How does auth work?\n\nMerged answer.\n\n## Sources\n- src/auth.py.md\n",
        ]
        log_fn = make_log_fn()

        result = deduplicate_query_pages(vault, llm, log_fn)

        # One group should be merged
        assert len(result.merged_groups) == 1
        surviving, deleted_list = result.merged_groups[0]
        assert len(deleted_list) == 1

        # The deleted page should no longer exist
        for deleted in deleted_list:
            assert not deleted.exists()

        # The surviving page should exist with merged content
        assert surviving.exists()
        content = surviving.read_text(encoding="utf-8")
        assert "Merged answer" in content

        # Log should have an entry for each deletion
        assert any("lint-deduplicated" in e for e in log_fn.entries)
