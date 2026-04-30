"""Unit tests for codebase_wiki_builder.analysis module."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_wiki_builder.analysis import (
    ANALYSIS_CONTEXT_WINDOW,
    AnalysisBatch,
    _build_partial_overview_prompt,
    _build_synthesis_prompt,
    build_batches,
    collect_summary_files,
)

logger = logging.getLogger("test_analysis")


# ---------------------------------------------------------------------------
# ANALYSIS_CONTEXT_WINDOW constant
# ---------------------------------------------------------------------------

class TestAnalysisContextWindow:
    def test_constant_value(self):
        assert ANALYSIS_CONTEXT_WINDOW == 64_000

    def test_is_int(self):
        assert isinstance(ANALYSIS_CONTEXT_WINDOW, int)


# ---------------------------------------------------------------------------
# collect_summary_files
# ---------------------------------------------------------------------------

class TestCollectSummaryFiles:
    def test_empty_vault(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        result = collect_summary_files(vault)
        assert result == []

    def test_collects_summary_files(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "main.py.md").write_text("# Summary", encoding="utf-8")

        result = collect_summary_files(vault)
        assert len(result) == 1
        assert result[0][1].name == "main.py.md"

    def test_excludes_special_files(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        for special in ("index.md", "log.md", "overview.md", "lint-report.md"):
            (vault / special).write_text("content", encoding="utf-8")
        (vault / "real.py.md").write_text("# Summary", encoding="utf-8")

        result = collect_summary_files(vault)
        assert len(result) == 1
        assert result[0][1].name == "real.py.md"

    def test_excludes_queries_dir(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        queries = vault / "queries"
        queries.mkdir()
        (queries / "how-auth-works.md").write_text("# Q", encoding="utf-8")
        (vault / "main.py.md").write_text("# Summary", encoding="utf-8")

        result = collect_summary_files(vault)
        files = [r[1].name for r in result]
        assert "how-auth-works.md" not in files
        assert "main.py.md" in files

    def test_excludes_logs_dir(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        logs = vault / "logs"
        logs.mkdir()
        (logs / "debug.log").write_text("log entry", encoding="utf-8")
        (vault / "code.py.md").write_text("# Summary", encoding="utf-8")

        result = collect_summary_files(vault)
        files = [r[1].name for r in result]
        assert "code.py.md" in files
        assert "debug.log" not in files

    def test_returns_vault_relative_dir(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        src_auth = vault / "src" / "auth"
        src_auth.mkdir(parents=True)
        (src_auth / "login.py.md").write_text("# Summary", encoding="utf-8")

        result = collect_summary_files(vault)
        assert len(result) == 1
        rel_dir, path = result[0]
        assert rel_dir == "src/auth"

    def test_root_level_files_have_empty_rel_dir(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "main.py.md").write_text("# Summary", encoding="utf-8")

        result = collect_summary_files(vault)
        assert result[0][0] == ""


# ---------------------------------------------------------------------------
# build_batches
# ---------------------------------------------------------------------------

class TestBuildBatches:
    def test_empty_files_returns_empty_batches(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        batches = build_batches([], vault, logger)
        assert batches == []

    def test_single_small_file_one_batch(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        f = vault / "main.py.md"
        f.write_text("# Summary\nShort content.", encoding="utf-8")

        summary_files = [("", f)]
        batches = build_batches(summary_files, vault, logger)
        assert len(batches) == 1
        assert isinstance(batches[0], AnalysisBatch)
        assert batches[0].file_paths == [f]

    def test_batch_has_correct_vault_dir(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        src = vault / "src"
        src.mkdir()
        f = src / "service.py.md"
        f.write_text("# Summary\nContent.", encoding="utf-8")

        summary_files = [("src", f)]
        batches = build_batches(summary_files, vault, logger)
        assert len(batches) == 1
        assert batches[0].vault_dir == "src"

    def test_files_in_different_top_dirs_create_multiple_batches(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        src = vault / "src"
        src.mkdir()
        tests = vault / "tests"
        tests.mkdir()

        f1 = src / "main.py.md"
        f1.write_text("content 1", encoding="utf-8")
        f2 = tests / "test_main.py.md"
        f2.write_text("content 2", encoding="utf-8")

        summary_files = [("src", f1), ("tests", f2)]
        batches = build_batches(summary_files, vault, logger)
        # Each top-level dir should get its own batch
        assert len(batches) == 2
        vault_dirs = {b.vault_dir for b in batches}
        assert vault_dirs == {"src", "tests"}

    def test_batch_token_count_is_populated(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        f = vault / "code.py.md"
        f.write_text("Some summary content here.", encoding="utf-8")

        summary_files = [("", f)]
        batches = build_batches(summary_files, vault, logger)
        assert batches[0].token_count > 0


# ---------------------------------------------------------------------------
# Prompt builders — f-string safety
# ---------------------------------------------------------------------------

class TestPromptBuildersFStringSafety:
    def test_partial_overview_prompt_with_curly_braces_in_content(self):
        """Prompt builder must NOT crash when summary content contains {curly_braces}."""
        content_with_braces = "Some {tricky} content with {curly_braces} inside."
        # Should not raise KeyError or any other exception
        prompt = _build_partial_overview_prompt("src/auth", content_with_braces)
        assert "{tricky}" in prompt
        assert "{curly_braces}" in prompt

    def test_synthesis_prompt_with_curly_braces_in_content(self):
        """Synthesis prompt builder must NOT crash with curly braces in overviews."""
        content_with_braces = "Overview with {curly} and {braces} content."
        prompt = _build_synthesis_prompt(content_with_braces)
        assert "{curly}" in prompt
        assert "{braces}" in prompt

    def test_partial_prompt_does_not_use_format(self):
        """Verify the prompt is not calling .format() on user content."""
        # If .format() were called, this would raise KeyError for undefined keys
        tricky = "{undefined_key}"
        try:
            prompt = _build_partial_overview_prompt("root", tricky)
            # If we get here, no KeyError was raised — f-strings are safe
            assert tricky in prompt
        except KeyError:
            pytest.fail("Prompt builder called .format() on untrusted content — f-string required!")

    def test_synthesis_prompt_does_not_use_format(self):
        """Verify synthesis prompt doesn't call .format() on overview text."""
        tricky = "{another_undefined_key_xyz}"
        try:
            prompt = _build_synthesis_prompt(tricky)
            assert tricky in prompt
        except KeyError:
            pytest.fail("Synthesis prompt builder called .format() on untrusted content!")
