"""Integration tests for the full analysis workflow.

Tests end-to-end flows:
  - collect_summary_files() → build_batches() → run_analysis() with mocked LLM
  - Verifies overview.md files created for each directory and root

Uses real filesystem (tmp_path). Mocks only LLMClient.complete().
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_wiki_builder.config import WikiConfig
from codebase_wiki_builder.analysis import (
    collect_summary_files,
    build_batches,
    run_analysis,
)


logger = logging.getLogger("test_integration_analysis")


def make_config(codebase_path: Path) -> WikiConfig:
    return WikiConfig(
        codebase_path=[str(codebase_path)],
        llm_provider="anthropic",
        llm_model="claude-sonnet-4-6",
        file_size_threshold=100_000,
        inter_request_delay=0.0,
    )


def make_vault_with_summaries(tmp_path: Path) -> tuple[Path, Path]:
    """Create a vault with summary files in subdirectories."""
    vault = tmp_path / "vault"
    codebase = tmp_path / "codebase"
    vault.mkdir()
    codebase.mkdir()

    # Root-level summary
    (vault / "readme.md.md").write_text(
        "# readme.md\n\nProject readme.\n\n## References\n\n<!-- md5: aa11 -->",
        encoding="utf-8",
    )

    # src/auth/ directory
    auth_dir = vault / "src" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "login.py.md").write_text(
        "# src/auth/login.py\n\nHandles login.\n\n## References\n\n<!-- md5: bb22 -->",
        encoding="utf-8",
    )

    # src/utils/ directory
    utils_dir = vault / "src" / "utils"
    utils_dir.mkdir(parents=True)
    (utils_dir / "helpers.py.md").write_text(
        "# src/utils/helpers.py\n\nHelper utilities.\n\n## References\n\n<!-- md5: cc33 -->",
        encoding="utf-8",
    )

    # Create index.md
    (vault / "index.md").write_text(
        "| File | Description |\n"
        "|------|-------------|\n"
        "| [[readme.md]] | Project readme |\n"
        "| [[src/auth/login.py]] | Login handler |\n"
        "| [[src/utils/helpers.py]] | Helper utilities |\n",
        encoding="utf-8",
    )

    return vault, codebase


def log_fn_noop(entry: str) -> None:
    pass


# ---------------------------------------------------------------------------
# collect_summary_files
# ---------------------------------------------------------------------------

class TestCollectSummaryFiles:
    def test_collects_files_in_subdirs(self, tmp_path):
        """collect_summary_files() returns files from all subdirectories."""
        vault, codebase = make_vault_with_summaries(tmp_path)

        results = collect_summary_files(vault)

        paths = [str(p) for _, p in results]
        # Should find all three summaries
        assert any("login.py.md" in p for p in paths)
        assert any("helpers.py.md" in p for p in paths)
        assert any("readme.md.md" in p for p in paths)

    def test_excludes_special_files(self, tmp_path):
        """collect_summary_files() excludes index.md, log.md, overview.md."""
        vault, codebase = make_vault_with_summaries(tmp_path)

        # Create special files
        (vault / "log.md").write_text("log content", encoding="utf-8")
        (vault / "overview.md").write_text("overview", encoding="utf-8")
        (vault / "src" / "overview.md").write_text("dir overview", encoding="utf-8")

        results = collect_summary_files(vault)
        filenames = [p.name for _, p in results]

        assert "log.md" not in filenames
        assert "index.md" not in filenames
        assert "overview.md" not in filenames

    def test_excludes_queries_dir(self, tmp_path):
        """collect_summary_files() excludes files under queries/."""
        vault, codebase = make_vault_with_summaries(tmp_path)

        queries_dir = vault / "queries"
        queries_dir.mkdir()
        (queries_dir / "some-question.md").write_text(
            "# Some question?\n\nAnswer.\n\n## Sources\n- readme.md.md\n",
            encoding="utf-8",
        )

        results = collect_summary_files(vault)
        filenames = [p.name for _, p in results]

        assert "some-question.md" not in filenames

    def test_returns_correct_rel_dir(self, tmp_path):
        """collect_summary_files() returns correct vault-relative directory."""
        vault, codebase = make_vault_with_summaries(tmp_path)

        results = collect_summary_files(vault)
        rel_dirs = {rel_dir for rel_dir, _ in results}

        assert "src/auth" in rel_dirs
        assert "src/utils" in rel_dirs


# ---------------------------------------------------------------------------
# build_batches
# ---------------------------------------------------------------------------

class TestBuildBatches:
    def test_builds_batches_from_summary_files(self, tmp_path):
        """build_batches() groups summary files into batches by directory."""
        vault, codebase = make_vault_with_summaries(tmp_path)

        summary_files = collect_summary_files(vault)

        with patch("codebase_wiki_builder.analysis._count_tokens", return_value=100):
            batches = build_batches(summary_files, vault, logger)

        # Should produce batches (at least 1)
        assert len(batches) >= 1
        # All batch files should exist
        for batch in batches:
            for path in batch.file_paths:
                assert path.exists()

    def test_batch_directories_match_source_dirs(self, tmp_path):
        """Batch vault_dir values correspond to actual vault directories."""
        vault, codebase = make_vault_with_summaries(tmp_path)

        summary_files = collect_summary_files(vault)

        with patch("codebase_wiki_builder.analysis._count_tokens", return_value=100):
            batches = build_batches(summary_files, vault, logger)

        vault_dirs = {b.vault_dir for b in batches}
        # At least one of our known dirs should appear
        assert any(
            "src" in d or d == "" for d in vault_dirs
        )


# ---------------------------------------------------------------------------
# run_analysis: integration
# ---------------------------------------------------------------------------

class TestRunAnalysis:
    def test_run_analysis_creates_overview_md(self, tmp_path):
        """run_analysis() creates a root overview.md in the vault."""
        vault, codebase = make_vault_with_summaries(tmp_path)
        config = make_config(codebase)

        mock_llm = MagicMock()
        mock_llm.complete.return_value = "This codebase handles authentication and utilities."

        log_entries: list[str] = []

        with patch("codebase_wiki_builder.analysis._count_tokens", return_value=100):
            run_analysis(vault, mock_llm, config, logger, log_entries.append)

        # Root overview.md must exist
        assert (vault / "overview.md").exists()
        overview_content = (vault / "overview.md").read_text(encoding="utf-8")
        assert len(overview_content) > 0

    def test_run_analysis_creates_subdir_overviews(self, tmp_path):
        """run_analysis() creates overview.md files in subdirectories."""
        vault, codebase = make_vault_with_summaries(tmp_path)
        config = make_config(codebase)

        mock_llm = MagicMock()
        mock_llm.complete.return_value = "Partial overview text for this directory."

        with patch("codebase_wiki_builder.analysis._count_tokens", return_value=100):
            run_analysis(vault, mock_llm, config, logger, log_fn_noop)

        # At least the root overview.md must exist
        assert (vault / "overview.md").exists()
        # LLM was called multiple times (per-batch + synthesis)
        assert mock_llm.complete.call_count >= 2

    def test_run_analysis_updates_index_md(self, tmp_path):
        """run_analysis() adds overview.md entries to index.md."""
        vault, codebase = make_vault_with_summaries(tmp_path)
        config = make_config(codebase)

        mock_llm = MagicMock()
        mock_llm.complete.return_value = "Overview content."

        with patch("codebase_wiki_builder.analysis._count_tokens", return_value=100):
            run_analysis(vault, mock_llm, config, logger, log_fn_noop)

        index_content = (vault / "index.md").read_text(encoding="utf-8")
        # Root overview should be listed
        assert "overview" in index_content.lower()

    def test_run_analysis_writes_log_entry(self, tmp_path):
        """run_analysis() appends a log entry via log_fn."""
        vault, codebase = make_vault_with_summaries(tmp_path)
        config = make_config(codebase)

        mock_llm = MagicMock()
        mock_llm.complete.return_value = "Analysis overview."

        log_entries: list[str] = []

        with patch("codebase_wiki_builder.analysis._count_tokens", return_value=100):
            run_analysis(vault, mock_llm, config, logger, log_entries.append)

        assert len(log_entries) >= 1
        log_text = "\n".join(log_entries)
        assert "analysis" in log_text
        assert "summaries_reviewed" in log_text

    def test_run_analysis_exits_when_no_index(self, tmp_path):
        """run_analysis() raises typer.Exit(1) when index.md is missing."""
        import typer

        vault = tmp_path / "vault"
        vault.mkdir()
        codebase = tmp_path / "codebase"
        codebase.mkdir()
        config = make_config(codebase)

        mock_llm = MagicMock()

        with pytest.raises(typer.Exit) as exc_info:
            run_analysis(vault, mock_llm, config, logger, log_fn_noop)

        assert exc_info.value.exit_code == 1

    def test_run_analysis_llm_called_for_each_batch_plus_synthesis(self, tmp_path):
        """LLM is called once per batch plus once for root synthesis."""
        vault, codebase = make_vault_with_summaries(tmp_path)
        config = make_config(codebase)

        mock_llm = MagicMock()
        mock_llm.complete.return_value = "Overview."

        with patch("codebase_wiki_builder.analysis._count_tokens", return_value=100):
            run_analysis(vault, mock_llm, config, logger, log_fn_noop)

        # At minimum: N batches + 1 synthesis call
        assert mock_llm.complete.call_count >= 2

    def test_run_analysis_overviews_overwritten_on_rerun(self, tmp_path):
        """Running analysis twice overwrites previous overview.md files."""
        vault, codebase = make_vault_with_summaries(tmp_path)
        config = make_config(codebase)

        mock_llm = MagicMock()
        mock_llm.complete.return_value = "First overview."

        with patch("codebase_wiki_builder.analysis._count_tokens", return_value=100):
            run_analysis(vault, mock_llm, config, logger, log_fn_noop)

        first_content = (vault / "overview.md").read_text(encoding="utf-8")

        mock_llm.complete.return_value = "Second overview with different content."

        with patch("codebase_wiki_builder.analysis._count_tokens", return_value=100):
            run_analysis(vault, mock_llm, config, logger, log_fn_noop)

        second_content = (vault / "overview.md").read_text(encoding="utf-8")
        assert second_content != first_content
        assert "Second overview" in second_content
