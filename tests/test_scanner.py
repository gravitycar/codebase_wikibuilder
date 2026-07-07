"""Unit tests for codebase_wiki_builder.scanner module."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_wiki_builder.config import WikiConfig
from codebase_wiki_builder.scanner import ChangeSet, scan_codebase
from codebase_wiki_builder.vault import md5_footer, compute_md5


logger = logging.getLogger("test_scanner")


def make_config(codebase_path: str, file_size_threshold: int = 100_000) -> WikiConfig:
    return WikiConfig(
        codebase_path=[codebase_path],
        file_size_threshold=file_size_threshold,
    )


# ---------------------------------------------------------------------------
# ChangeSet construction
# ---------------------------------------------------------------------------

class TestChangeSetDefaults:
    def test_all_lists_empty_by_default(self):
        cs = ChangeSet()
        assert cs.new_files == []
        assert cs.modified_files == []
        assert cs.deleted_summaries == []
        assert cs.skipped_too_large == []
        assert cs.skipped_binary == []
        assert cs.skipped_unchanged == []


# ---------------------------------------------------------------------------
# scan_codebase — new files
# ---------------------------------------------------------------------------

class TestScanCodebaseNewFiles:
    def test_new_file_detected(self, tmp_path):
        # Arrange: codebase with one text file, vault is empty
        codebase = tmp_path / "app"
        codebase.mkdir()
        vault = tmp_path / "vault"
        vault.mkdir()
        (codebase / "main.py").write_text("print('hello')", encoding="utf-8")

        config = make_config(str(codebase))

        # Act
        cs = scan_codebase(config, vault, logger)

        # Assert
        assert len(cs.new_files) == 1
        assert cs.new_files[0].name == "main.py"
        assert cs.modified_files == []
        assert cs.skipped_binary == []
        assert cs.skipped_unchanged == []

    def test_multiple_new_files(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        vault = tmp_path / "vault"
        vault.mkdir()
        for name in ("a.py", "b.py", "c.py"):
            (codebase / name).write_text("code", encoding="utf-8")

        config = make_config(str(codebase))
        cs = scan_codebase(config, vault, logger)

        assert len(cs.new_files) == 3
        assert cs.modified_files == []

    def test_nested_new_file(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        vault = tmp_path / "vault"
        vault.mkdir()
        (codebase / "src" / "auth").mkdir(parents=True)
        (codebase / "src" / "auth" / "login.py").write_text("# login", encoding="utf-8")

        config = make_config(str(codebase))
        cs = scan_codebase(config, vault, logger)

        assert len(cs.new_files) == 1
        assert cs.new_files[0].name == "login.py"


# ---------------------------------------------------------------------------
# scan_codebase — binary detection
# ---------------------------------------------------------------------------

class TestScanCodebaseBinary:
    def test_png_is_skipped_as_binary(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        vault = tmp_path / "vault"
        vault.mkdir()
        (codebase / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")

        config = make_config(str(codebase))
        cs = scan_codebase(config, vault, logger)

        assert len(cs.skipped_binary) == 1
        assert cs.new_files == []

    def test_null_byte_file_is_binary(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        vault = tmp_path / "vault"
        vault.mkdir()
        (codebase / "data.bin").write_bytes(b"text\x00binary")

        config = make_config(str(codebase))
        cs = scan_codebase(config, vault, logger)

        assert len(cs.skipped_binary) == 1

    def test_invalid_utf8_is_binary(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        vault = tmp_path / "vault"
        vault.mkdir()
        (codebase / "latin1.txt").write_bytes(b"\xff\xfe\x00\x41")

        config = make_config(str(codebase))
        cs = scan_codebase(config, vault, logger)

        assert len(cs.skipped_binary) == 1


# ---------------------------------------------------------------------------
# scan_codebase — too large
# ---------------------------------------------------------------------------

class TestScanCodebaseTooLarge:
    def test_oversized_file_skipped(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        vault = tmp_path / "vault"
        vault.mkdir()
        # Create a file that exceeds the threshold
        big_file = codebase / "large.py"
        big_file.write_bytes(b"x" * 200)

        config = make_config(str(codebase), file_size_threshold=100)
        cs = scan_codebase(config, vault, logger)

        assert len(cs.skipped_too_large) == 1
        assert cs.new_files == []


# ---------------------------------------------------------------------------
# scan_codebase — unchanged detection
# ---------------------------------------------------------------------------

class TestScanCodebaseUnchanged:
    def test_unchanged_file_skipped(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        vault = tmp_path / "vault"
        vault.mkdir()

        source = codebase / "main.py"
        source.write_text("print('hello')", encoding="utf-8")

        # Create a vault summary with matching MD5 footer
        md5 = compute_md5(source)
        summary_dir = vault
        summary_dir.mkdir(exist_ok=True)
        summary = summary_dir / "main.py.md"
        summary.write_text(f"# Summary\n\nContent\n{md5_footer(md5)}", encoding="utf-8")

        config = make_config(str(codebase))
        cs = scan_codebase(config, vault, logger)

        assert len(cs.skipped_unchanged) == 1
        assert cs.new_files == []
        assert cs.modified_files == []


# ---------------------------------------------------------------------------
# scan_codebase — modified detection
# ---------------------------------------------------------------------------

class TestScanCodebaseModified:
    def test_modified_file_detected(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        vault = tmp_path / "vault"
        vault.mkdir()

        source = codebase / "main.py"
        source.write_text("print('hello')", encoding="utf-8")

        # Create a vault summary with STALE MD5 footer
        stale_md5 = "a" * 32
        summary = vault / "main.py.md"
        summary.write_text(f"# Summary\n\nOld content\n{md5_footer(stale_md5)}", encoding="utf-8")

        config = make_config(str(codebase))
        cs = scan_codebase(config, vault, logger)

        assert len(cs.modified_files) == 1
        assert cs.modified_files[0].name == "main.py"
        assert cs.new_files == []


# ---------------------------------------------------------------------------
# scan_codebase — deleted summaries
# ---------------------------------------------------------------------------

class TestScanCodebaseDeleted:
    def test_deleted_summary_detected(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        vault = tmp_path / "vault"
        vault.mkdir()

        # Create a vault summary for a source file that no longer exists
        orphan_summary = vault / "gone.py.md"
        orphan_summary.write_text("# Summary\n\nStale content\n", encoding="utf-8")

        config = make_config(str(codebase))
        cs = scan_codebase(config, vault, logger)

        assert len(cs.deleted_summaries) == 1
        assert cs.deleted_summaries[0].name == "gone.py.md"

    def test_special_files_not_marked_deleted(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        vault = tmp_path / "vault"
        vault.mkdir()

        # Create special files that should be excluded from deletion detection
        (vault / "index.md").write_text("# Index", encoding="utf-8")
        (vault / "log.md").write_text("# Log", encoding="utf-8")
        (vault / "overview.md").write_text("# Overview", encoding="utf-8")

        config = make_config(str(codebase))
        cs = scan_codebase(config, vault, logger)

        assert cs.deleted_summaries == []

    def test_excluded_dirs_not_walked(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        vault = tmp_path / "vault"
        vault.mkdir()

        # Create a queries/ dir with a file — should not be scanned for deletions
        queries_dir = vault / "queries"
        queries_dir.mkdir()
        (queries_dir / "how-auth-works.md").write_text("# Q\n## Sources\n- src/auth.py.md", encoding="utf-8")

        config = make_config(str(codebase))
        cs = scan_codebase(config, vault, logger)

        assert cs.deleted_summaries == []


# ---------------------------------------------------------------------------
# scan_codebase — excluded source dirs
# ---------------------------------------------------------------------------

class TestScanCodebaseExcludedDirs:
    def test_git_dir_excluded(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        vault = tmp_path / "vault"
        vault.mkdir()

        git_dir = codebase / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("[core]", encoding="utf-8")

        config = make_config(str(codebase))
        cs = scan_codebase(config, vault, logger)

        assert cs.new_files == []

    def test_node_modules_excluded(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        vault = tmp_path / "vault"
        vault.mkdir()

        nm = codebase / "node_modules"
        nm.mkdir()
        (nm / "index.js").write_text("module.exports = {}", encoding="utf-8")

        config = make_config(str(codebase))
        cs = scan_codebase(config, vault, logger)

        assert cs.new_files == []


# ---------------------------------------------------------------------------
# scan_codebase — multiple paths
# ---------------------------------------------------------------------------

class TestScanCodebaseMultiplePaths:
    def test_two_directories_both_scanned(self, tmp_path):
        dir_a = tmp_path / "src"
        dir_b = tmp_path / "frontend"
        dir_a.mkdir()
        dir_b.mkdir()
        vault = tmp_path / "vault"
        vault.mkdir()
        (dir_a / "api.py").write_text("# api", encoding="utf-8")
        (dir_b / "App.vue").write_text("<template/>", encoding="utf-8")

        config = WikiConfig(codebase_path=[str(dir_a), str(dir_b)])
        cs = scan_codebase(config, vault, logger)

        names = {f.name for f in cs.new_files}
        assert names == {"api.py", "App.vue"}

    def test_specific_file_entry_ingested(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        vault = tmp_path / "vault"
        vault.mkdir()
        (codebase / "config.php").write_text("<?php", encoding="utf-8")
        (codebase / "index.html").write_text("<html/>", encoding="utf-8")

        # Only ingest config.php, not index.html
        config = WikiConfig(codebase_path=[str(codebase / "config.php")])
        cs = scan_codebase(config, vault, logger)

        assert len(cs.new_files) == 1
        assert cs.new_files[0].name == "config.php"

    def test_directory_and_file_combined(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "service.py").write_text("# service", encoding="utf-8")
        config_file = tmp_path / "config.php"
        config_file.write_text("<?php", encoding="utf-8")
        vault = tmp_path / "vault"
        vault.mkdir()

        config = WikiConfig(codebase_path=[str(src), str(config_file)])
        cs = scan_codebase(config, vault, logger)

        names = {f.name for f in cs.new_files}
        assert names == {"service.py", "config.php"}

    def test_excluded_dirs_not_walked_in_multi_path(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "api.py").write_text("# api", encoding="utf-8")
        vendor = tmp_path / "vendor"
        vendor.mkdir()
        (vendor / "lib.php").write_text("<?php", encoding="utf-8")
        vault = tmp_path / "vault"
        vault.mkdir()

        # Only include src, not vendor
        config = WikiConfig(codebase_path=[str(src)])
        cs = scan_codebase(config, vault, logger)

        assert len(cs.new_files) == 1
        assert cs.new_files[0].name == "api.py"
