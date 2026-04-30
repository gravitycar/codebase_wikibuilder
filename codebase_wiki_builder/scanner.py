"""File discovery and change-set computation for Codebase Wiki Builder.

Implements Phase 1 of the ingest command: scanning the target codebase to
compute the full change-set (new, modified, deleted, skipped files) without
making any changes to the vault.

The public entry point is scan_codebase(), which returns a ChangeSet dataclass
containing all information needed by Phase 2 (summarizer, deletions, index,
staleness detection).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from codebase_wiki_builder.config import WikiConfig
from codebase_wiki_builder.vault import (
    EXCLUDED_DIRS,
    VAULT_EXCLUDED_DIRS,
    VAULT_SPECIAL_FILES,
    compute_md5,
    extract_stored_md5,
    is_binary_file,
    source_path_from_vault,
    vault_path_for_source,
)


# ---------------------------------------------------------------------------
# ChangeSet dataclass
# ---------------------------------------------------------------------------

@dataclass
class ChangeSet:
    # Source files (absolute paths under codebase_root) that have no
    # corresponding vault summary yet — must be summarized.
    new_files: list[Path] = field(default_factory=list)

    # Source files whose current MD5 differs from the stored MD5 in
    # the existing vault summary — must be re-summarized.
    modified_files: list[Path] = field(default_factory=list)

    # Vault summary paths (absolute, under vault_root) whose corresponding
    # source file no longer exists in the codebase — must be deleted in Phase 2.
    deleted_summaries: list[Path] = field(default_factory=list)

    # Source files skipped because they exceed the configured size threshold.
    # Stored for progress display and log.md reporting.
    skipped_too_large: list[Path] = field(default_factory=list)

    # Source files skipped because they are binary (extension match,
    # null-byte, or UTF-8 decode failure).
    skipped_binary: list[Path] = field(default_factory=list)

    # Source files skipped because they already have an up-to-date summary
    # (current MD5 matches stored MD5). Not written to log.md but used for
    # progress display.
    skipped_unchanged: list[Path] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def scan_codebase(
    config: WikiConfig,
    vault_root: Path,
    logger: logging.Logger,
) -> ChangeSet:
    """Compute the full Phase 1 change-set without making any vault writes.

    Parameters
    ----------
    config:
        WikiConfig instance (provides codebase_path and file_size_threshold).
    vault_root:
        Absolute Path to the vault root directory.
    logger:
        Application logger; used for DEBUG-level per-file events and WARNING
        for oversized / unreadable files.

    Returns
    -------
    ChangeSet
        Fully-populated change-set. Never raises; all per-file errors are
        logged and the affected file is treated as skipped.
    """
    codebase_root = Path(config.codebase_path)
    change_set = ChangeSet()

    # --- Pass 1: classify source files ---
    _discover_source_files(codebase_root, vault_root, config, change_set, logger)

    # --- Pass 2: detect deleted summaries ---
    _detect_deleted_summaries(vault_root, codebase_root, change_set, logger)

    logger.info(
        "Phase 1 complete: new=%d modified=%d deleted=%d "
        "unchanged=%d too_large=%d binary=%d",
        len(change_set.new_files),
        len(change_set.modified_files),
        len(change_set.deleted_summaries),
        len(change_set.skipped_unchanged),
        len(change_set.skipped_too_large),
        len(change_set.skipped_binary),
    )
    return change_set


# ---------------------------------------------------------------------------
# Pass 1 helpers
# ---------------------------------------------------------------------------

def _discover_source_files(
    codebase_root: Path,
    vault_root: Path,
    config: WikiConfig,
    change_set: ChangeSet,
    logger: logging.Logger,
) -> None:
    """Walk the codebase and classify each file into the appropriate ChangeSet list."""
    for dirpath, dirnames, filenames in os.walk(codebase_root):
        # Prune excluded directories in-place (modifies dirnames before os.walk descends)
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]

        current_dir = Path(dirpath)
        for filename in filenames:
            file = current_dir / filename
            _classify_source_file(file, codebase_root, vault_root, config, change_set, logger)


def _classify_source_file(
    file: Path,
    codebase_root: Path,
    vault_root: Path,
    config: WikiConfig,
    change_set: ChangeSet,
    logger: logging.Logger,
) -> None:
    """Classify a single source file and append it to the appropriate ChangeSet list.

    Filter chain: binary check → size check → MD5 comparison.
    All OSError conditions are caught, logged, and treated as skipped_binary.
    """
    # Binary check
    if is_binary_file(file):
        logger.debug("Skipping binary: %s", file)
        change_set.skipped_binary.append(file)
        return

    # Size check
    try:
        size = file.stat().st_size
    except OSError as exc:
        logger.warning("Cannot stat %s: %s — treating as binary/skip", file, exc)
        change_set.skipped_binary.append(file)
        return

    if size > config.file_size_threshold:
        logger.warning(
            "Skipping oversized file (%d bytes > %d): %s",
            size, config.file_size_threshold, file,
        )
        change_set.skipped_too_large.append(file)
        return

    # MD5 comparison — guard against rare race condition
    try:
        current_md5 = compute_md5(file)
    except OSError as exc:
        logger.warning("Cannot read %s for MD5 computation: %s — skipping", file, exc)
        change_set.skipped_binary.append(file)
        return

    vault_summary = vault_path_for_source(file, codebase_root, vault_root)
    stored_md5 = extract_stored_md5(vault_summary)

    if stored_md5 is None:
        logger.debug("New file (no summary): %s", file)
        change_set.new_files.append(file)
    elif stored_md5 == current_md5:
        logger.debug("Unchanged (MD5 match): %s", file)
        change_set.skipped_unchanged.append(file)
    else:
        logger.debug("Modified (MD5 mismatch): %s", file)
        change_set.modified_files.append(file)


# ---------------------------------------------------------------------------
# Pass 2 helper
# ---------------------------------------------------------------------------

def _detect_deleted_summaries(
    vault_root: Path,
    codebase_root: Path,
    change_set: ChangeSet,
    logger: logging.Logger,
) -> None:
    """Walk the vault and detect summary files whose source no longer exists.

    Excludes vault-root special files (index.md, log.md, overview.md,
    lint-report.md), any overview.md in subdirectories, and all files under
    the logs/ and queries/ directories.
    """
    for dirpath, dirnames, filenames in os.walk(vault_root):
        # Prune logs/ and queries/ from vault walk
        dirnames[:] = [d for d in dirnames if d not in VAULT_EXCLUDED_DIRS]

        current_dir = Path(dirpath)
        for filename in filenames:
            if not filename.endswith(".md"):
                continue

            # Skip vault-root special files
            if current_dir == vault_root and filename in VAULT_SPECIAL_FILES:
                continue

            # Skip any overview.md in subdirectories
            if filename == "overview.md":
                continue

            summary_path = current_dir / filename
            try:
                source_path = source_path_from_vault(summary_path, vault_root, codebase_root)
            except ValueError as exc:
                logger.error(
                    "Cannot determine source path for vault summary %s: %s — skipping",
                    summary_path, exc,
                )
                continue

            if not source_path.exists():
                logger.debug(
                    "Deleted summary detected: %s (source gone: %s)",
                    summary_path, source_path,
                )
                change_set.deleted_summaries.append(summary_path)
