"""Unit tests for codebase_wiki_builder.deletion module."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from codebase_wiki_builder.deletion import (
    DeletionResult,
    _build_dead_wikilinks,
    _cleanup_empty_directories,
    _delete_summary_files,
    _remove_backlinks_from_file,
    apply_deletions,
)
from codebase_wiki_builder.scanner import ChangeSet

logger = logging.getLogger("test_deletion")


def make_log_fn():
    entries = []
    def log_fn(entry: str) -> None:
        entries.append(entry)
    log_fn.entries = entries
    return log_fn


# ---------------------------------------------------------------------------
# apply_deletions — no-op when no deletions
# ---------------------------------------------------------------------------

class TestApplyDeletionsNoop:
    def test_returns_empty_result_when_no_deletions(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        change_set = ChangeSet()
        log_fn = make_log_fn()

        result = apply_deletions(change_set, vault, log_fn, logger)

        assert result.deleted_files == []
        assert result.failed_deletions == []
        assert result.backlinks_cleaned == []
        assert result.removed_dirs == []


# ---------------------------------------------------------------------------
# apply_deletions — summary file deletion
# ---------------------------------------------------------------------------

class TestApplyDeletionsSummaryFiles:
    def test_deletes_existing_summary_file(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        summary = vault / "gone.py.md"
        summary.write_text("# Summary\n\nStale.", encoding="utf-8")

        change_set = ChangeSet(deleted_summaries=[summary])
        log_fn = make_log_fn()

        result = apply_deletions(change_set, vault, log_fn, logger)

        assert summary not in [p for p in vault.iterdir()]
        assert summary in result.deleted_files
        assert len(log_fn.entries) >= 1
        assert "deletion" in log_fn.entries[0]

    def test_idempotent_when_file_already_gone(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        # File doesn't exist — deletion should treat as success (idempotent)
        nonexistent = vault / "nonexistent.py.md"

        change_set = ChangeSet(deleted_summaries=[nonexistent])
        log_fn = make_log_fn()

        result = apply_deletions(change_set, vault, log_fn, logger)

        assert nonexistent in result.deleted_files
        assert result.failed_deletions == []


# ---------------------------------------------------------------------------
# Backlink removal
# ---------------------------------------------------------------------------

class TestBacklinkRemoval:
    def test_removes_dead_backlink_from_references_section(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        # Create the summary that will be deleted
        deleted_summary = vault / "gone.py.md"
        deleted_summary.write_text("# Gone\n\nContent.", encoding="utf-8")

        # Create a remaining summary with a backlink to the deleted file
        remaining_summary = vault / "other.py.md"
        remaining_summary.write_text(
            "# Other\n\nContent.\n\n## References\n- [[gone.py]]\n",
            encoding="utf-8",
        )

        change_set = ChangeSet(deleted_summaries=[deleted_summary])
        log_fn = make_log_fn()

        result = apply_deletions(change_set, vault, log_fn, logger)

        content = remaining_summary.read_text(encoding="utf-8")
        assert "[[gone.py]]" not in content
        assert len(result.backlinks_cleaned) == 1
        assert result.backlinks_cleaned[0][1] == 1  # 1 link removed

    def test_does_not_remove_links_outside_references_section(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        deleted_summary = vault / "gone.py.md"
        deleted_summary.write_text("# Gone", encoding="utf-8")

        # Link in body text (not in ## References) should be preserved
        remaining_summary = vault / "other.py.md"
        remaining_summary.write_text(
            "# Other\n\nSee [[gone.py]] for details.\n\n## References\n- something else\n",
            encoding="utf-8",
        )

        change_set = ChangeSet(deleted_summaries=[deleted_summary])
        log_fn = make_log_fn()

        result = apply_deletions(change_set, vault, log_fn, logger)

        content = remaining_summary.read_text(encoding="utf-8")
        # Link in body should remain
        assert "See [[gone.py]] for details." in content


# ---------------------------------------------------------------------------
# _build_dead_wikilinks
# ---------------------------------------------------------------------------

class TestBuildDeadWikilinks:
    def test_generates_wikilink_from_vault_path(self, tmp_path):
        vault = tmp_path / "vault"
        deleted = vault / "src" / "auth.py.md"
        dead = _build_dead_wikilinks([deleted], vault)
        assert "[[src/auth.py]]" in dead

    def test_root_level_summary(self, tmp_path):
        vault = tmp_path / "vault"
        deleted = vault / "main.py.md"
        dead = _build_dead_wikilinks([deleted], vault)
        assert "[[main.py]]" in dead


# ---------------------------------------------------------------------------
# _cleanup_empty_directories
# ---------------------------------------------------------------------------

class TestCleanupEmptyDirectories:
    def test_removes_empty_directory(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        empty_dir = vault / "src" / "auth"
        empty_dir.mkdir(parents=True)

        removed = _cleanup_empty_directories(vault, logger)

        # Both src/auth and src should have been removed (bottom-up)
        assert not empty_dir.exists()
        assert not (vault / "src").exists()
        assert len(removed) >= 2

    def test_does_not_remove_non_empty_directory(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        non_empty = vault / "src"
        non_empty.mkdir()
        (non_empty / "main.py.md").write_text("content", encoding="utf-8")

        removed = _cleanup_empty_directories(vault, logger)

        assert non_empty.exists()
        assert removed == []

    def test_does_not_remove_vault_root(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        _cleanup_empty_directories(vault, logger)

        # Vault root must still exist
        assert vault.exists()

    def test_does_not_remove_excluded_dirs(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        # queries/ is in VAULT_EXCLUDED_DIRS — should never be removed even when empty
        queries = vault / "queries"
        queries.mkdir()

        removed = _cleanup_empty_directories(vault, logger)

        # queries/ should still exist (it's excluded from removal)
        assert queries.exists()
        assert queries not in removed
