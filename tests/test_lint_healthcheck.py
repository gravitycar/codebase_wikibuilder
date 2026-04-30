"""Unit tests for codebase_wiki_builder.lint_healthcheck module."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_wiki_builder.lint_healthcheck import (
    _build_batch_health_check_prompt,
    _build_health_check_synthesis_prompt,
    _read_index_content,
    _write_lint_report,
    run_health_check,
)

logger = logging.getLogger("test_lint_healthcheck")


def make_log_fn():
    entries = []
    def log_fn(entry: str) -> None:
        entries.append(entry)
    log_fn.entries = entries
    return log_fn


# ---------------------------------------------------------------------------
# _build_batch_health_check_prompt — f-string safety
# ---------------------------------------------------------------------------

class TestBuildBatchHealthCheckPromptFStringSafety:
    def test_curly_braces_in_index_no_crash(self):
        index_content = "| [[src/{config}.py]] | Some {template} |\n"
        combined_summaries = "Normal content"
        try:
            prompt = _build_batch_health_check_prompt(index_content, combined_summaries)
            assert "{config}" in prompt
        except KeyError:
            pytest.fail("Batch health-check prompt builder called .format()!")

    def test_curly_braces_in_summaries_no_crash(self):
        index_content = "| File | Description |\n"
        combined_summaries = "Uses {env_var} for {config_key}."
        try:
            prompt = _build_batch_health_check_prompt(index_content, combined_summaries)
            assert "{env_var}" in prompt
        except KeyError:
            pytest.fail("Batch health-check prompt builder called .format()!")


# ---------------------------------------------------------------------------
# _build_health_check_synthesis_prompt — f-string safety
# ---------------------------------------------------------------------------

class TestBuildHealthCheckSynthesisPromptFStringSafety:
    def test_curly_braces_in_batch_findings_no_crash(self):
        batch_findings = "## Orphan Pages\nFile {orphan}.md has no {backlinks}."
        try:
            prompt = _build_health_check_synthesis_prompt(batch_findings)
            assert "{orphan}" in prompt
        except KeyError:
            pytest.fail("Synthesis prompt builder called .format() on findings text!")


# ---------------------------------------------------------------------------
# _read_index_content
# ---------------------------------------------------------------------------

class TestReadIndexContent:
    def test_reads_index_content(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "index.md").write_text("# Index\n\n| File | Desc |\n", encoding="utf-8")

        result = _read_index_content(vault, logger)
        assert "# Index" in result

    def test_returns_empty_string_when_no_index(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        result = _read_index_content(vault, logger)
        assert result == ""


# ---------------------------------------------------------------------------
# _write_lint_report
# ---------------------------------------------------------------------------

class TestWriteLintReport:
    def test_creates_lint_report_md(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        synthesis = (
            "## Orphan Pages\nNone found.\n\n"
            "## Missing Cross-References\nNone found.\n\n"
            "## Contradictions\nNone found.\n\n"
            "## Concept Gaps\nNone found."
        )
        _write_lint_report(vault, synthesis, dedup_entries=[], log=logger)

        assert (vault / "lint-report.md").exists()

    def test_report_contains_synthesis(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        synthesis = "## Orphan Pages\nSome orphan found.\n"
        _write_lint_report(vault, synthesis, dedup_entries=[], log=logger)

        content = (vault / "lint-report.md").read_text(encoding="utf-8")
        assert "Some orphan found." in content

    def test_report_contains_dedup_entries(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        synthesis = "## Orphan Pages\nNone found.\n"
        dedup_entries = ["queries/old.md → queries/new.md"]
        _write_lint_report(vault, synthesis, dedup_entries=dedup_entries, log=logger)

        content = (vault / "lint-report.md").read_text(encoding="utf-8")
        assert "queries/old.md → queries/new.md" in content

    def test_report_has_header(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        _write_lint_report(vault, "synthesis content", dedup_entries=[], log=logger)

        content = (vault / "lint-report.md").read_text(encoding="utf-8")
        assert "# Wiki Lint Report" in content
        assert "Generated:" in content

    def test_report_shows_none_when_no_dedup_entries(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        _write_lint_report(vault, "synthesis", dedup_entries=[], log=logger)

        content = (vault / "lint-report.md").read_text(encoding="utf-8")
        assert "None" in content  # dedup section should say "None"


# ---------------------------------------------------------------------------
# run_health_check — with no summary files
# ---------------------------------------------------------------------------

class TestRunHealthCheckNoSummaryFiles:
    def test_writes_minimal_report_when_no_summaries(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        llm = MagicMock()
        log_fn = make_log_fn()

        run_health_check(vault, llm, log_fn)

        # LLM should not have been called (no summaries to batch)
        llm.complete.assert_not_called()
        assert (vault / "lint-report.md").exists()

    def test_minimal_report_contains_four_sections(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        llm = MagicMock()
        log_fn = make_log_fn()

        run_health_check(vault, llm, log_fn)

        content = (vault / "lint-report.md").read_text(encoding="utf-8")
        assert "## Orphan Pages" in content
        assert "## Missing Cross-References" in content
        assert "## Contradictions" in content
        assert "## Concept Gaps" in content


# ---------------------------------------------------------------------------
# run_health_check — with summary files (mocked LLM)
# ---------------------------------------------------------------------------

class TestRunHealthCheckWithSummaryFiles:
    def test_calls_llm_for_each_batch(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        # Create one summary file
        (vault / "main.py.md").write_text("# main.py\n\nEntry point.", encoding="utf-8")

        llm = MagicMock()
        # First call: per-batch findings; second call: synthesis
        per_batch_findings = (
            "## Orphan Pages\nNone found.\n\n"
            "## Missing Cross-References\nNone found.\n\n"
            "## Contradictions\nNone found.\n\n"
            "## Concept Gaps\nNone found."
        )
        llm.complete.side_effect = [per_batch_findings, per_batch_findings]
        log_fn = make_log_fn()

        run_health_check(vault, llm, log_fn)

        # LLM should have been called at least once
        assert llm.complete.call_count >= 1
        assert (vault / "lint-report.md").exists()

    def test_includes_dedup_result_in_report(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        (vault / "main.py.md").write_text("# main.py\n\nEntry point.", encoding="utf-8")

        llm = MagicMock()
        llm.complete.return_value = (
            "## Orphan Pages\nNone found.\n\n"
            "## Missing Cross-References\nNone found.\n\n"
            "## Contradictions\nNone found.\n\n"
            "## Concept Gaps\nNone found."
        )
        log_fn = make_log_fn()

        # Build a fake dedup result
        from codebase_wiki_builder.lint_dedup import LintDedupResult

        surviving = vault / "queries" / "new.md"
        deleted = vault / "queries" / "old.md"
        dedup_result = LintDedupResult(
            merged_groups=[(surviving, [deleted])],
            skipped_pages=[],
        )

        run_health_check(vault, llm, log_fn, dedup_result=dedup_result)

        content = (vault / "lint-report.md").read_text(encoding="utf-8")
        assert "queries/old.md" in content
        assert "queries/new.md" in content

    def test_detects_issues_in_report(self, tmp_path):
        """run_health_check writes LLM findings into lint-report.md correctly."""
        vault = tmp_path / "vault"
        vault.mkdir()

        (vault / "auth.py.md").write_text("# auth.py\n\nHandles tokens.", encoding="utf-8")

        llm = MagicMock()
        per_batch = (
            "## Orphan Pages\nauth.py.md has no inbound links.\n\n"
            "## Missing Cross-References\nNone found.\n\n"
            "## Contradictions\nNone found.\n\n"
            "## Concept Gaps\nNone found."
        )
        synthesis = (
            "## Orphan Pages\nauth.py.md has no inbound links.\n\n"
            "## Missing Cross-References\nNone found.\n\n"
            "## Contradictions\nNone found.\n\n"
            "## Concept Gaps\nNone found."
        )
        llm.complete.side_effect = [per_batch, synthesis]
        log_fn = make_log_fn()

        run_health_check(vault, llm, log_fn)

        content = (vault / "lint-report.md").read_text(encoding="utf-8")
        assert "auth.py.md" in content
