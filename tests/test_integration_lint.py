"""Integration tests for the full lint workflow.

Tests end-to-end flows:
  - Stale resolution: stale query page → resolve_stale_pages() → banner removed
  - Deduplication: two near-identical query pages → deduplicate_query_pages() → one merged
  - Health-check: run_health_check() → lint-report.md written with required sections

Uses real filesystem (tmp_path). Mocks only LLMClient.complete().
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_wiki_builder.config import WikiConfig
from codebase_wiki_builder.lint_staleness import resolve_stale_pages, LintStalenessResult
from codebase_wiki_builder.lint_dedup import deduplicate_query_pages, LintDedupResult
from codebase_wiki_builder.lint_healthcheck import run_health_check
from codebase_wiki_builder.query_engine import QueryResult


logger = logging.getLogger("test_integration_lint")


def make_config(codebase_path: Path) -> WikiConfig:
    return WikiConfig(
        codebase_path=[str(codebase_path)],
        llm_provider="anthropic",
        llm_model="claude-sonnet-4-6",
        file_size_threshold=100_000,
        inter_request_delay=0.0,
    )


def log_fn_noop(entry: str) -> None:
    pass


def make_vault_with_summaries(tmp_path: Path) -> tuple[Path, Path]:
    """Create a vault with summary files and an index.md."""
    vault = tmp_path / "vault"
    codebase = tmp_path / "codebase"
    vault.mkdir()
    codebase.mkdir()

    src_dir = vault / "src"
    src_dir.mkdir()

    (src_dir / "auth.py.md").write_text(
        "# src/auth.py\n\nHandles authentication.\n\n## References\n\n"
        "<!-- md5: aabb1122aabb1122aabb1122aabb1122 -->",
        encoding="utf-8",
    )
    (src_dir / "utils.py.md").write_text(
        "# src/utils.py\n\nUtility functions.\n\n## References\n\n"
        "<!-- md5: ccdd3344ccdd3344ccdd3344ccdd3344 -->",
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


# ---------------------------------------------------------------------------
# Staleness resolution integration
# ---------------------------------------------------------------------------

class TestStalenessResolution:
    def test_resolve_stale_page_removes_banner(self, tmp_path, monkeypatch):
        """resolve_stale_pages() rewrites a stale page without the stale banner."""
        vault, codebase = make_vault_with_summaries(tmp_path)
        config = make_config(codebase)

        # Create a stale query page
        queries_dir = vault / "queries"
        queries_dir.mkdir()
        page_path = queries_dir / "how-does-auth-work.md"
        page_path.write_text(
            "# How does auth work?\n\n"
            "> [!warning] Stale Content\n"
            "> src/auth.py.md changed.\n"
            "> Run `codewiki lint` to regenerate this answer.\n\n"
            "Old answer about auth.\n\n"
            "## Sources\n"
            "- src/auth.py.md\n\n"
            "## Page Metadata\n"
            "saved_at: 2026-04-29 10:00:00 UTC\n"
            "updated_at: 2026-04-29 10:00:00 UTC\n",
            encoding="utf-8",
        )

        # Add stale annotation to index
        index_content = (vault / "index.md").read_text(encoding="utf-8")
        index_content += "| [[queries/how-does-auth-work]] | Explains auth ⚠ stale |\n"
        (vault / "index.md").write_text(index_content, encoding="utf-8")

        # Mock run_query to return a fresh result
        fresh_result = QueryResult(
            answer="Fresh auth answer.\n\n## Sources\n- src/auth.py.md",
            sources=["src/auth.py.md"],
            one_line_summary="Explains how JWT auth works",
            stale_warnings=[],
        )

        mock_llm = MagicMock()
        log_entries: list[str] = []

        with patch("codebase_wiki_builder.lint_staleness._run_internal_query", return_value=fresh_result):
            result = resolve_stale_pages(vault, mock_llm, config, log_entries.append)

        # (a) Page overwritten — no stale banner
        content = page_path.read_text(encoding="utf-8")
        assert "> [!warning] Stale Content" not in content
        assert "Fresh auth answer." in content

        # (b) H1 is still the first line
        assert content.splitlines()[0] == "# How does auth work?"

        # (c) Index annotation removed
        index_updated = (vault / "index.md").read_text(encoding="utf-8")
        assert "⚠ stale" not in index_updated

        # (d) lint-query and lint-resolved in log
        log_text = "\n".join(log_entries)
        assert "lint-query" in log_text
        assert "lint-resolved" in log_text
        assert "query-saved" not in log_text

        # (f) updated_at changed from the original
        assert "saved_at: 2026-04-29 10:00:00 UTC" in content
        # The updated_at should NOT be the old timestamp
        assert "updated_at: 2026-04-29 10:00:00 UTC" not in content

        # (g) saved_at preserved
        assert "saved_at: 2026-04-29 10:00:00 UTC" in content

        # Result
        assert len(result.resolved_pages) == 1
        assert len(result.unknowable_pages) == 0

    def test_resolve_stale_page_unknowable(self, tmp_path):
        """When re-query returns no relevant files, page is flagged unknowable."""
        vault, codebase = make_vault_with_summaries(tmp_path)
        config = make_config(codebase)

        queries_dir = vault / "queries"
        queries_dir.mkdir()
        page_path = queries_dir / "how-does-feature-x-work.md"
        page_path.write_text(
            "# How does feature X work?\n\n"
            "> [!warning] Stale Content\n"
            "> Sources changed.\n\n"
            "Old answer about feature X.\n\n"
            "## Sources\n"
            "- src/feature_x.py.md\n\n"
            "## Page Metadata\n"
            "saved_at: 2026-04-29 10:00:00 UTC\n"
            "updated_at: 2026-04-29 10:00:00 UTC\n",
            encoding="utf-8",
        )

        index_content = (vault / "index.md").read_text(encoding="utf-8")
        index_content += "| [[queries/how-does-feature-x-work]] | Explains feature X ⚠ stale |\n"
        (vault / "index.md").write_text(index_content, encoding="utf-8")

        mock_llm = MagicMock()
        log_entries: list[str] = []

        # Simulate: run_query returns no relevant files (None = unknowable in _run_internal_query)
        with patch(
            "codebase_wiki_builder.lint_staleness._run_internal_query",
            return_value=None,
        ):
            result = resolve_stale_pages(vault, mock_llm, config, log_entries.append)

        content = page_path.read_text(encoding="utf-8")

        # (a) Canonical answer text
        assert "this question cannot be answered by the wiki or the codebase" in content

        # (b) Unknowable banner after H1
        lines = content.splitlines()
        assert lines[0] == "# How does feature X work?"
        assert "> [!error] Unknowable" in content

        # (c) index shows ⊘ unknowable
        index_updated = (vault / "index.md").read_text(encoding="utf-8")
        assert "⊘ unknowable" in index_updated
        assert "⚠ stale" not in index_updated

        # (d) lint-unknowable in log
        log_text = "\n".join(log_entries)
        assert "lint-unknowable" in log_text

        # (e) lint did not abort
        assert len(result.unknowable_pages) == 1
        assert len(result.resolved_pages) == 0

    def test_lint_continues_after_unknowable(self, tmp_path):
        """Lint processes all stale pages even if one is unknowable."""
        vault, codebase = make_vault_with_summaries(tmp_path)
        config = make_config(codebase)

        queries_dir = vault / "queries"
        queries_dir.mkdir()

        # Two stale pages
        for slug, question in [
            ("unknowable-page", "What is unknowable?"),
            ("resolvable-page", "What is resolvable?"),
        ]:
            (queries_dir / f"{slug}.md").write_text(
                f"# {question}\n\n"
                "> [!warning] Stale Content\n"
                "> Sources changed.\n\n"
                f"Old answer about {slug}.\n\n"
                "## Sources\n"
                "- src/auth.py.md\n\n"
                "## Page Metadata\n"
                "saved_at: 2026-01-01 00:00:00 UTC\n"
                "updated_at: 2026-01-01 00:00:00 UTC\n",
                encoding="utf-8",
            )

        index_content = (vault / "index.md").read_text(encoding="utf-8")
        index_content += (
            "| [[queries/unknowable-page]] | Unknowable ⚠ stale |\n"
            "| [[queries/resolvable-page]] | Resolvable ⚠ stale |\n"
        )
        (vault / "index.md").write_text(index_content, encoding="utf-8")

        fresh_result = QueryResult(
            answer="Fresh answer.\n\n## Sources\n- src/auth.py.md",
            sources=["src/auth.py.md"],
            one_line_summary="Fresh summary",
            stale_warnings=[],
        )

        call_count = [0]

        def mock_internal_query(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return None  # unknowable: _run_internal_query returns None for NoRelevantFilesError
            return fresh_result

        mock_llm = MagicMock()

        with patch("codebase_wiki_builder.lint_staleness._run_internal_query", side_effect=mock_internal_query):
            result = resolve_stale_pages(vault, mock_llm, config, log_fn_noop)

        # Both pages processed
        assert len(result.unknowable_pages) == 1
        assert len(result.resolved_pages) == 1
        assert call_count[0] == 2

    def test_resolve_no_stale_pages(self, tmp_path):
        """resolve_stale_pages() handles vault with no stale pages gracefully."""
        vault, codebase = make_vault_with_summaries(tmp_path)
        config = make_config(codebase)

        mock_llm = MagicMock()

        result = resolve_stale_pages(vault, mock_llm, config, log_fn_noop)

        assert len(result.resolved_pages) == 0
        assert len(result.unknowable_pages) == 0
        assert len(result.skipped_pages) == 0


# ---------------------------------------------------------------------------
# Deduplication integration
# ---------------------------------------------------------------------------

class TestDeduplication:
    def test_deduplicate_merges_duplicate_query_pages(self, tmp_path):
        """deduplicate_query_pages() removes duplicate pages and leaves merged page."""
        vault, codebase = make_vault_with_summaries(tmp_path)

        queries_dir = vault / "queries"
        queries_dir.mkdir()

        # Create two near-duplicate query pages
        page1 = queries_dir / "how-does-auth-work.md"
        page2 = queries_dir / "explain-authentication.md"

        page1.write_text(
            "# How does auth work?\n\n"
            "Auth uses JWT tokens.\n\n"
            "## Sources\n- src/auth.py.md\n\n"
            "## Page Metadata\n"
            "saved_at: 2026-04-29 10:00:00 UTC\n"
            "updated_at: 2026-04-29 10:00:00 UTC\n",
            encoding="utf-8",
        )
        page2.write_text(
            "# Explain authentication\n\n"
            "Authentication is done with JWT.\n\n"
            "## Sources\n- src/auth.py.md\n\n"
            "## Page Metadata\n"
            "saved_at: 2026-04-29 11:00:00 UTC\n"
            "updated_at: 2026-04-29 11:00:00 UTC\n",
            encoding="utf-8",
        )

        # Update index.md to include both query pages
        index_content = (vault / "index.md").read_text(encoding="utf-8")
        index_content += (
            "| [[queries/how-does-auth-work]] | Explains how authentication works |\n"
            "| [[queries/explain-authentication]] | Describes the authentication system |\n"
        )
        (vault / "index.md").write_text(index_content, encoding="utf-8")

        mock_llm = MagicMock()

        # LLM calls:
        # 1. Detection call: returns duplicate group
        # 2. Merge call: returns merged content
        mock_llm.complete.side_effect = [
            # Detection pass: identify duplicates as a JSON array of groups
            json.dumps([
                ["queries/how-does-auth-work.md", "queries/explain-authentication.md"]
            ]),
            # Merge pass: return merged page content
            json.dumps({
                "merged_answer": "Auth uses JWT tokens. This is the unified explanation.\n\n## Sources\n- src/auth.py.md",
                "one_line_summary": "Unified explanation of JWT authentication",
            }),
        ]

        log_entries: list[str] = []
        result = deduplicate_query_pages(vault, mock_llm, log_entries.append)

        # One group was merged
        assert len(result.merged_groups) == 1
        surviving_page, deleted_pages = result.merged_groups[0]

        # Exactly one page was deleted
        assert len(deleted_pages) == 1

        # The deleted page no longer exists on disk
        for deleted in deleted_pages:
            assert not deleted.exists()

        # The surviving page still exists
        assert surviving_page.exists()

        # index.md should have only one entry for the merged group
        index_updated = (vault / "index.md").read_text(encoding="utf-8")
        surviving_slug = surviving_page.stem
        assert surviving_slug in index_updated

        # log entries contain lint-deduplicated
        log_text = "\n".join(log_entries)
        assert "lint-deduplicated" in log_text

    def test_deduplicate_skips_when_fewer_than_two_query_pages(self, tmp_path):
        """deduplicate_query_pages() skips dedup when < 2 query pages exist."""
        vault, codebase = make_vault_with_summaries(tmp_path)

        queries_dir = vault / "queries"
        queries_dir.mkdir()

        page = queries_dir / "single-query.md"
        page.write_text(
            "# Single query?\n\n"
            "Answer.\n\n"
            "## Sources\n- src/auth.py.md\n\n"
            "## Page Metadata\n"
            "saved_at: 2026-04-29 10:00:00 UTC\n"
            "updated_at: 2026-04-29 10:00:00 UTC\n",
            encoding="utf-8",
        )

        index_content = (vault / "index.md").read_text(encoding="utf-8")
        index_content += "| [[queries/single-query]] | Single query answer |\n"
        (vault / "index.md").write_text(index_content, encoding="utf-8")

        mock_llm = MagicMock()
        result = deduplicate_query_pages(vault, mock_llm, log_fn_noop)

        # LLM should not be called (< 2 pages)
        assert mock_llm.complete.call_count == 0
        assert len(result.merged_groups) == 0

    def test_deduplicate_no_duplicates_detected(self, tmp_path):
        """deduplicate_query_pages() handles case where LLM finds no duplicates."""
        vault, codebase = make_vault_with_summaries(tmp_path)

        queries_dir = vault / "queries"
        queries_dir.mkdir()

        for slug, question in [
            ("query-about-auth", "How does auth work?"),
            ("query-about-utils", "What do utils do?"),
        ]:
            (queries_dir / f"{slug}.md").write_text(
                f"# {question}\n\n"
                "Different topic answer.\n\n"
                "## Sources\n- src/auth.py.md\n\n"
                "## Page Metadata\n"
                "saved_at: 2026-04-29 10:00:00 UTC\n"
                "updated_at: 2026-04-29 10:00:00 UTC\n",
                encoding="utf-8",
            )

        index_content = (vault / "index.md").read_text(encoding="utf-8")
        index_content += (
            "| [[queries/query-about-auth]] | Auth explanation |\n"
            "| [[queries/query-about-utils]] | Utils explanation |\n"
        )
        (vault / "index.md").write_text(index_content, encoding="utf-8")

        mock_llm = MagicMock()
        # LLM returns empty array — no duplicates
        mock_llm.complete.return_value = json.dumps([])

        result = deduplicate_query_pages(vault, mock_llm, log_fn_noop)

        assert len(result.merged_groups) == 0
        # Both pages still exist
        assert (queries_dir / "query-about-auth.md").exists()
        assert (queries_dir / "query-about-utils.md").exists()


# ---------------------------------------------------------------------------
# Health-check integration
# ---------------------------------------------------------------------------

class TestHealthCheck:
    def test_health_check_writes_lint_report(self, tmp_path):
        """run_health_check() writes a lint-report.md with all four section headers."""
        vault, codebase = make_vault_with_summaries(tmp_path)

        mock_llm = MagicMock()
        mock_llm.complete.return_value = (
            "## Orphan Pages\nNone found.\n\n"
            "## Missing Cross-References\nNone found.\n\n"
            "## Contradictions\nNone found.\n\n"
            "## Concept Gaps\nNone found.\n"
        )

        dedup_result = MagicMock()
        dedup_result.merged_groups = []

        with patch("codebase_wiki_builder.analysis._count_tokens", return_value=100):
            run_health_check(vault, mock_llm, log_fn_noop, dedup_result)

        # lint-report.md must exist
        assert (vault / "lint-report.md").exists()

        report_content = (vault / "lint-report.md").read_text(encoding="utf-8")
        assert len(report_content) > 0

        # All four section headers required by AT-17
        assert "## Orphan Pages" in report_content
        assert "## Missing Cross-References" in report_content
        assert "## Contradictions" in report_content
        assert "## Concept Gaps" in report_content

    def test_health_check_contains_generated_timestamp(self, tmp_path):
        """lint-report.md contains a 'Generated:' timestamp."""
        vault, codebase = make_vault_with_summaries(tmp_path)

        mock_llm = MagicMock()
        mock_llm.complete.return_value = (
            "## Orphan Pages\nNone.\n\n"
            "## Missing Cross-References\nNone.\n\n"
            "## Contradictions\nNone.\n\n"
            "## Concept Gaps\nNone.\n"
        )

        dedup_result = MagicMock()
        dedup_result.merged_groups = []

        with patch("codebase_wiki_builder.analysis._count_tokens", return_value=100):
            run_health_check(vault, mock_llm, log_fn_noop, dedup_result)

        report_content = (vault / "lint-report.md").read_text(encoding="utf-8")
        assert "Generated:" in report_content

    def test_health_check_overwrites_on_rerun(self, tmp_path):
        """Running health check twice overwrites the previous lint-report.md."""
        vault, codebase = make_vault_with_summaries(tmp_path)

        mock_llm = MagicMock()

        dedup_result = MagicMock()
        dedup_result.merged_groups = []

        mock_llm.complete.return_value = (
            "## Orphan Pages\nFirst run findings.\n\n"
            "## Missing Cross-References\nNone.\n\n"
            "## Contradictions\nNone.\n\n"
            "## Concept Gaps\nNone.\n"
        )
        with patch("codebase_wiki_builder.analysis._count_tokens", return_value=100):
            run_health_check(vault, mock_llm, log_fn_noop, dedup_result)

        first_content = (vault / "lint-report.md").read_text(encoding="utf-8")

        mock_llm.complete.return_value = (
            "## Orphan Pages\nSecond run findings.\n\n"
            "## Missing Cross-References\nNone.\n\n"
            "## Contradictions\nNone.\n\n"
            "## Concept Gaps\nNone.\n"
        )
        with patch("codebase_wiki_builder.analysis._count_tokens", return_value=100):
            run_health_check(vault, mock_llm, log_fn_noop, dedup_result)

        second_content = (vault / "lint-report.md").read_text(encoding="utf-8")
        assert "Second run findings." in second_content

    def test_health_check_handles_empty_vault(self, tmp_path):
        """run_health_check() runs without crash even if vault has no summary files."""
        vault = tmp_path / "vault"
        vault.mkdir()

        # Create minimal index.md
        (vault / "index.md").write_text(
            "| File | Description |\n|------|-------------|\n",
            encoding="utf-8",
        )

        mock_llm = MagicMock()
        mock_llm.complete.return_value = (
            "## Orphan Pages\nNone found.\n\n"
            "## Missing Cross-References\nNone found.\n\n"
            "## Contradictions\nNone found.\n\n"
            "## Concept Gaps\nNone found.\n"
        )

        dedup_result = MagicMock()
        dedup_result.merged_groups = []

        with patch("codebase_wiki_builder.analysis._count_tokens", return_value=100):
            # Should not raise
            run_health_check(vault, mock_llm, log_fn_noop, dedup_result)

        assert (vault / "lint-report.md").exists()
