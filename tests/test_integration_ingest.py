"""Integration tests for the full ingest workflow.

Tests end-to-end flows:
  - Fresh ingest: scan → summarize → write → rebuild_index
  - Incremental ingest: unchanged files skipped, modified files re-summarized
  - Deletion: deleted source file → summary removed → index updated
  - Staleness detection: modified source → query page flagged stale

Uses real filesystem (tmp_path). Mocks only LLMClient.complete().
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_wiki_builder.config import WikiConfig
from codebase_wiki_builder.scanner import scan_codebase, ChangeSet
from codebase_wiki_builder.summarizer import summarize_file, write_summary
from codebase_wiki_builder.deletion import apply_deletions
from codebase_wiki_builder.index_writer import rebuild_index
from codebase_wiki_builder.staleness import detect_stale_queries
from codebase_wiki_builder.vault import vault_path_for_source, compute_md5


logger = logging.getLogger("test_integration_ingest")


def make_config(codebase_path: Path) -> WikiConfig:
    """Build a WikiConfig pointing at the given codebase path."""
    return WikiConfig(
        codebase_path=str(codebase_path),
        llm_provider="anthropic",
        llm_model="claude-sonnet-4-6",
        file_size_threshold=100_000,
        inter_request_delay=0.0,
    )


def make_mock_llm(summary_text: str | None = None) -> MagicMock:
    """Return a mock LLMClient whose .complete() returns a valid JSON summary."""
    mock_llm = MagicMock()
    response_json = json.dumps({
        "description": summary_text or "This file does X.",
        "explicit_references": [],
        "dynamic_references": [],
    })
    mock_llm.complete.return_value = f"```json\n{response_json}\n```"
    return mock_llm


def log_fn_noop(entry: str) -> None:
    """No-op log function for tests that don't need log.md."""
    pass


# ---------------------------------------------------------------------------
# Fresh ingest: scan → summarize → write → rebuild_index
# ---------------------------------------------------------------------------

class TestFreshIngest:
    def test_fresh_ingest_creates_summary_files(self, tmp_path):
        """After fresh ingest, a summary file exists for each source file."""
        vault = tmp_path / "vault"
        codebase = tmp_path / "codebase"
        vault.mkdir()
        codebase.mkdir()

        # Create 3 source files
        (codebase / "a.py").write_text("print('a')", encoding="utf-8")
        (codebase / "b.py").write_text("print('b')", encoding="utf-8")
        src_dir = codebase / "src"
        src_dir.mkdir()
        (src_dir / "c.py").write_text("print('c')", encoding="utf-8")

        config = make_config(codebase)
        mock_llm = make_mock_llm()

        # Phase 1: scan
        change_set = scan_codebase(config, vault, logger)
        assert len(change_set.new_files) == 3
        assert len(change_set.modified_files) == 0

        # Phase 2: summarize and write
        for source_file in change_set.new_files:
            summary_str = summarize_file(source_file, mock_llm, config, vault, logger)
            vault_path = vault_path_for_source(source_file, codebase, vault)
            write_summary(vault_path, summary_str)

        # Rebuild index
        rebuild_index(vault, logger)

        # Verify: summary files exist
        assert (vault / "a.py.md").exists()
        assert (vault / "b.py.md").exists()
        assert (vault / "src" / "c.py.md").exists()

        # Verify: index.md exists and lists all 3 summaries
        index_content = (vault / "index.md").read_text(encoding="utf-8")
        assert "a.py" in index_content
        assert "b.py" in index_content
        assert "c.py" in index_content

    def test_fresh_ingest_summary_has_md5_footer(self, tmp_path):
        """Each summary file has an MD5 footer matching the source file's current hash."""
        vault = tmp_path / "vault"
        codebase = tmp_path / "codebase"
        vault.mkdir()
        codebase.mkdir()

        source_file = codebase / "main.py"
        source_file.write_text("def main(): pass", encoding="utf-8")

        config = make_config(codebase)
        mock_llm = make_mock_llm()

        change_set = scan_codebase(config, vault, logger)
        for f in change_set.new_files:
            summary_str = summarize_file(f, mock_llm, config, vault, logger)
            write_summary(vault_path_for_source(f, codebase, vault), summary_str)

        summary_path = vault / "main.py.md"
        assert summary_path.exists()
        content = summary_path.read_text(encoding="utf-8")

        # MD5 footer pattern
        import re
        md5_match = re.search(r"<!--\s*md5:\s*([a-f0-9]{32})\s*-->", content)
        assert md5_match is not None, "No MD5 footer found in summary"

        # Footer hash matches the source file's actual MD5
        actual_md5 = compute_md5(source_file)
        assert md5_match.group(1) == actual_md5

    def test_fresh_ingest_index_md_table_format(self, tmp_path):
        """index.md uses the two-column markdown table format."""
        vault = tmp_path / "vault"
        codebase = tmp_path / "codebase"
        vault.mkdir()
        codebase.mkdir()

        (codebase / "app.py").write_text("# app", encoding="utf-8")

        config = make_config(codebase)
        mock_llm = make_mock_llm()

        change_set = scan_codebase(config, vault, logger)
        for f in change_set.new_files:
            summary_str = summarize_file(f, mock_llm, config, vault, logger)
            write_summary(vault_path_for_source(f, codebase, vault), summary_str)
        rebuild_index(vault, logger)

        index_content = (vault / "index.md").read_text(encoding="utf-8")
        lines = index_content.splitlines()

        # Header row
        assert lines[0].startswith("| File")
        assert "Description" in lines[0]
        # Separator row
        assert lines[1].startswith("|---")
        # At least one data row with a wikilink
        data_rows = [l for l in lines[2:] if l.strip().startswith("|")]
        assert len(data_rows) >= 1
        assert "[[" in data_rows[0]

    def test_fresh_ingest_binary_files_excluded(self, tmp_path):
        """Binary files (.png) are not summarized."""
        vault = tmp_path / "vault"
        codebase = tmp_path / "codebase"
        vault.mkdir()
        codebase.mkdir()

        (codebase / "main.py").write_text("print('hello')", encoding="utf-8")
        (codebase / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00")

        config = make_config(codebase)
        change_set = scan_codebase(config, vault, logger)

        source_names = [f.name for f in change_set.new_files]
        assert "main.py" in source_names
        assert "image.png" not in source_names

        binary_names = [f.name for f in change_set.skipped_binary]
        assert "image.png" in binary_names


# ---------------------------------------------------------------------------
# Incremental ingest: unchanged files skipped, modified files re-summarized
# ---------------------------------------------------------------------------

class TestIncrementalIngest:
    def test_incremental_ingest_skips_unchanged_files(self, tmp_path):
        """On re-run, files with matching MD5 are skipped."""
        vault = tmp_path / "vault"
        codebase = tmp_path / "codebase"
        vault.mkdir()
        codebase.mkdir()

        (codebase / "unchanged.py").write_text("def foo(): pass", encoding="utf-8")
        (codebase / "changed.py").write_text("original content", encoding="utf-8")

        config = make_config(codebase)
        mock_llm = make_mock_llm()

        # First ingest
        change_set1 = scan_codebase(config, vault, logger)
        for f in change_set1.new_files:
            summary_str = summarize_file(f, mock_llm, config, vault, logger)
            write_summary(vault_path_for_source(f, codebase, vault), summary_str)
        rebuild_index(vault, logger)

        assert mock_llm.complete.call_count == 2  # both files summarized

        # Modify one file
        (codebase / "changed.py").write_text("modified content", encoding="utf-8")
        mock_llm.complete.reset_mock()

        # Second ingest
        change_set2 = scan_codebase(config, vault, logger)

        assert len(change_set2.new_files) == 0
        assert len(change_set2.modified_files) == 1
        assert len(change_set2.skipped_unchanged) == 1

        modified_names = [f.name for f in change_set2.modified_files]
        assert "changed.py" in modified_names
        unchanged_names = [f.name for f in change_set2.skipped_unchanged]
        assert "unchanged.py" in unchanged_names

        # Re-summarize only modified files
        for f in change_set2.modified_files:
            summary_str = summarize_file(f, mock_llm, config, vault, logger)
            write_summary(vault_path_for_source(f, codebase, vault), summary_str)

        # Only 1 LLM call on second ingest
        assert mock_llm.complete.call_count == 1

    def test_incremental_ingest_updates_summary_md5(self, tmp_path):
        """After re-summarizing a modified file, the stored MD5 matches the new content."""
        vault = tmp_path / "vault"
        codebase = tmp_path / "codebase"
        vault.mkdir()
        codebase.mkdir()

        source_file = codebase / "myfile.py"
        source_file.write_text("original", encoding="utf-8")

        config = make_config(codebase)
        mock_llm = make_mock_llm()

        # First ingest
        change_set1 = scan_codebase(config, vault, logger)
        for f in change_set1.new_files:
            summary_str = summarize_file(f, mock_llm, config, vault, logger)
            write_summary(vault_path_for_source(f, codebase, vault), summary_str)

        # Modify file
        source_file.write_text("modified content version 2", encoding="utf-8")

        # Second ingest
        change_set2 = scan_codebase(config, vault, logger)
        for f in change_set2.modified_files:
            summary_str = summarize_file(f, mock_llm, config, vault, logger)
            write_summary(vault_path_for_source(f, codebase, vault), summary_str)

        # Check summary has new MD5
        import re
        summary_path = vault / "myfile.py.md"
        content = summary_path.read_text(encoding="utf-8")
        md5_match = re.search(r"<!--\s*md5:\s*([a-f0-9]{32})\s*-->", content)
        assert md5_match is not None

        new_md5 = compute_md5(source_file)
        assert md5_match.group(1) == new_md5


# ---------------------------------------------------------------------------
# Deletion: deleted source file → summary removed → index updated
# ---------------------------------------------------------------------------

class TestDeletion:
    def test_deletion_removes_summary_file(self, tmp_path):
        """When a source file is deleted, its summary is removed from the vault."""
        vault = tmp_path / "vault"
        codebase = tmp_path / "codebase"
        vault.mkdir()
        codebase.mkdir()

        file_a = codebase / "a.py"
        file_b = codebase / "b.py"
        file_a.write_text("def a(): pass", encoding="utf-8")
        file_b.write_text("def b(): pass", encoding="utf-8")

        config = make_config(codebase)
        mock_llm = make_mock_llm()

        # First ingest
        change_set1 = scan_codebase(config, vault, logger)
        for f in change_set1.new_files:
            summary_str = summarize_file(f, mock_llm, config, vault, logger)
            write_summary(vault_path_for_source(f, codebase, vault), summary_str)
        rebuild_index(vault, logger)

        # Verify both summaries exist
        assert (vault / "a.py.md").exists()
        assert (vault / "b.py.md").exists()

        # Delete source file b.py
        file_b.unlink()

        # Second scan
        change_set2 = scan_codebase(config, vault, logger)
        assert len(change_set2.deleted_summaries) == 1
        assert change_set2.deleted_summaries[0].name == "b.py.md"

        # Apply deletions
        deletion_result = apply_deletions(change_set2, vault, log_fn_noop, logger)
        assert len(deletion_result.deleted_files) == 1

        # Rebuild index
        rebuild_index(vault, logger)

        # Verify b.py.md is gone from vault
        assert not (vault / "b.py.md").exists()
        # a.py.md still present
        assert (vault / "a.py.md").exists()

        # Verify index.md no longer mentions b.py
        index_content = (vault / "index.md").read_text(encoding="utf-8")
        assert "b.py" not in index_content
        assert "a.py" in index_content

    def test_deletion_removes_backlinks(self, tmp_path):
        """When a source file is deleted, backlinks to it are removed from other summaries."""
        vault = tmp_path / "vault"
        codebase = tmp_path / "codebase"
        vault.mkdir()
        codebase.mkdir()

        file_a = codebase / "a.py"
        file_b = codebase / "b.py"
        file_a.write_text("def a(): pass", encoding="utf-8")
        file_b.write_text("import a", encoding="utf-8")

        config = make_config(codebase)
        mock_llm = make_mock_llm()

        # Write a.py.md and b.py.md with b referencing a
        change_set1 = scan_codebase(config, vault, logger)
        for f in change_set1.new_files:
            summary_str = summarize_file(f, mock_llm, config, vault, logger)
            write_summary(vault_path_for_source(f, codebase, vault), summary_str)

        # Manually add a backlink from b.py.md to a.py.md
        b_summary = vault / "b.py.md"
        b_content = b_summary.read_text(encoding="utf-8")
        b_content = b_content.replace("## References", "## References\n- [[a.py]]")
        b_summary.write_text(b_content, encoding="utf-8")

        rebuild_index(vault, logger)

        # Delete a.py
        file_a.unlink()

        change_set2 = scan_codebase(config, vault, logger)
        deletion_result = apply_deletions(change_set2, vault, log_fn_noop, logger)

        # The backlink to a.py should be removed from b.py.md
        b_new_content = b_summary.read_text(encoding="utf-8")
        assert "[[a.py]]" not in b_new_content


# ---------------------------------------------------------------------------
# Staleness detection: modified source → query page flagged stale
# ---------------------------------------------------------------------------

class TestStalenessDetection:
    def test_staleness_detection_flags_query_page(self, tmp_path):
        """After modifying a source file, query pages referencing it are flagged stale."""
        vault = tmp_path / "vault"
        codebase = tmp_path / "codebase"
        vault.mkdir()
        codebase.mkdir()

        source_file = codebase / "auth.py"
        source_file.write_text("def login(): pass", encoding="utf-8")

        config = make_config(codebase)
        mock_llm = make_mock_llm()

        # First ingest
        change_set1 = scan_codebase(config, vault, logger)
        for f in change_set1.new_files:
            summary_str = summarize_file(f, mock_llm, config, vault, logger)
            write_summary(vault_path_for_source(f, codebase, vault), summary_str)
        rebuild_index(vault, logger)

        # Create a query page referencing auth.py.md
        queries_dir = vault / "queries"
        queries_dir.mkdir()
        query_page = queries_dir / "how-does-auth-work.md"
        query_page.write_text(
            "# How does auth work?\n\n"
            "Authentication uses login().\n\n"
            "## Sources\n"
            "- auth.py.md\n\n"
            "## Page Metadata\n"
            "saved_at: 2026-04-29 10:00:00 UTC\n"
            "updated_at: 2026-04-29 10:00:00 UTC\n",
            encoding="utf-8",
        )

        # Update index.md to include the query page row
        index_content = (vault / "index.md").read_text(encoding="utf-8")
        index_content += "| [[queries/how-does-auth-work]] | Explains auth |\n"
        (vault / "index.md").write_text(index_content, encoding="utf-8")

        # Modify the source file to trigger staleness
        source_file.write_text("def login(): raise NotImplementedError()", encoding="utf-8")

        # Phase 1: scan again
        change_set2 = scan_codebase(config, vault, logger)
        assert len(change_set2.modified_files) == 1

        # Staleness detection
        log_entries: list[str] = []
        staleness_result = detect_stale_queries(
            change_set2, vault, codebase, log_entries.append, logger
        )

        # Query page should be flagged stale
        assert len(staleness_result.flagged_pages) == 1
        assert staleness_result.flagged_pages[0].name == "how-does-auth-work.md"

        # Stale banner should be inserted in the query page
        query_content = query_page.read_text(encoding="utf-8")
        assert "> [!warning] Stale Content" in query_content

        # H1 title should remain the first line
        first_line = query_content.splitlines()[0]
        assert first_line == "# How does auth work?"

        # index.md should have ⚠ stale annotation
        index_updated = (vault / "index.md").read_text(encoding="utf-8")
        assert "⚠ stale" in index_updated

        # log.md entry should mention query-stale
        log_text = "\n".join(log_entries)
        assert "query-stale" in log_text

    def test_staleness_no_duplicate_banner(self, tmp_path):
        """Running staleness detection twice does not add a second stale banner."""
        vault = tmp_path / "vault"
        codebase = tmp_path / "codebase"
        vault.mkdir()
        codebase.mkdir()

        source_file = codebase / "service.py"
        source_file.write_text("class Service: pass", encoding="utf-8")

        config = make_config(codebase)
        mock_llm = make_mock_llm()

        # Initial ingest
        change_set1 = scan_codebase(config, vault, logger)
        for f in change_set1.new_files:
            summary_str = summarize_file(f, mock_llm, config, vault, logger)
            write_summary(vault_path_for_source(f, codebase, vault), summary_str)
        rebuild_index(vault, logger)

        # Create query page
        queries_dir = vault / "queries"
        queries_dir.mkdir()
        query_page = queries_dir / "about-service.md"
        query_page.write_text(
            "# About service?\n\n"
            "Service does things.\n\n"
            "## Sources\n"
            "- service.py.md\n\n"
            "## Page Metadata\n"
            "saved_at: 2026-04-29 10:00:00 UTC\n"
            "updated_at: 2026-04-29 10:00:00 UTC\n",
            encoding="utf-8",
        )
        index_content = (vault / "index.md").read_text(encoding="utf-8")
        index_content += "| [[queries/about-service]] | About service |\n"
        (vault / "index.md").write_text(index_content, encoding="utf-8")

        # Modify source to trigger staleness
        source_file.write_text("class Service: updated = True", encoding="utf-8")

        change_set2 = scan_codebase(config, vault, logger)

        # First staleness detection run
        detect_stale_queries(change_set2, vault, codebase, log_fn_noop, logger)

        # Second staleness detection run (no changes since first run)
        # The page already has a stale banner — should not add another one
        detect_stale_queries(change_set2, vault, codebase, log_fn_noop, logger)

        # Verify only one stale banner block
        query_content = query_page.read_text(encoding="utf-8")
        banner_count = query_content.count("> [!warning] Stale Content")
        assert banner_count == 1, f"Expected 1 stale banner, found {banner_count}"

    def test_staleness_deleted_source_flags_query(self, tmp_path):
        """When a referenced source file is deleted, the query page is flagged stale."""
        vault = tmp_path / "vault"
        codebase = tmp_path / "codebase"
        vault.mkdir()
        codebase.mkdir()

        source_file = codebase / "old_module.py"
        source_file.write_text("# old module", encoding="utf-8")

        config = make_config(codebase)
        mock_llm = make_mock_llm()

        # First ingest
        change_set1 = scan_codebase(config, vault, logger)
        for f in change_set1.new_files:
            summary_str = summarize_file(f, mock_llm, config, vault, logger)
            write_summary(vault_path_for_source(f, codebase, vault), summary_str)
        rebuild_index(vault, logger)

        # Create query page referencing old_module.py.md
        queries_dir = vault / "queries"
        queries_dir.mkdir()
        query_page = queries_dir / "about-old-module.md"
        query_page.write_text(
            "# About old module?\n\n"
            "It does old things.\n\n"
            "## Sources\n"
            "- old_module.py.md\n\n"
            "## Page Metadata\n"
            "saved_at: 2026-04-29 10:00:00 UTC\n"
            "updated_at: 2026-04-29 10:00:00 UTC\n",
            encoding="utf-8",
        )
        index_content = (vault / "index.md").read_text(encoding="utf-8")
        index_content += "| [[queries/about-old-module]] | About old module |\n"
        (vault / "index.md").write_text(index_content, encoding="utf-8")

        # Delete the source file
        source_file.unlink()

        # Scan again — should detect deletion
        change_set2 = scan_codebase(config, vault, logger)
        assert len(change_set2.deleted_summaries) == 1

        # Apply deletions FIRST (Phase 2 ordering)
        apply_deletions(change_set2, vault, log_fn_noop, logger)

        # Now run staleness detection with the change_set that includes deleted paths
        staleness_result = detect_stale_queries(
            change_set2, vault, codebase, log_fn_noop, logger
        )

        # The query page should be flagged stale because old_module.py.md was deleted
        assert len(staleness_result.flagged_pages) == 1
