"""Deletion handling and backlink cleanup for Codebase Wiki Builder.

Implements Phase 2 deletion step of the ingest command (FR-3.7, FR-3.1):
  1. Delete vault summary files whose source is gone.
  2. Scan all remaining summaries for dead backlinks and remove them.
  3. Remove vault directories that become empty after deletions.
  4. Log each deletion and backlink removal to log.md and the debug logger.

The public entry point is apply_deletions(). This module is a pure filesystem
mutator — it receives fully-computed information from the scanner (ChangeSet)
and vault utilities (wikilink) and applies destructive changes to the vault.
It does not call the LLM.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from codebase_wiki_builder.scanner import ChangeSet
from codebase_wiki_builder.vault import VAULT_EXCLUDED_DIRS, VAULT_SPECIAL_FILES, wikilink


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class DeletionResult:
    # Vault summary paths that were successfully deleted
    deleted_files: list[Path] = field(default_factory=list)

    # Vault summary paths that could not be deleted (OSError)
    failed_deletions: list[Path] = field(default_factory=list)

    # (vault_summary_path, count_of_backlinks_removed) for each file modified
    backlinks_cleaned: list[tuple[Path, int]] = field(default_factory=list)

    # Vault directories removed because they became empty
    removed_dirs: list[Path] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    """Return current UTC time formatted for log.md entries."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _delete_summary_files(
    deleted_summaries: list[Path],
    log_fn: Callable[[str], None],
    logger: logging.Logger,
) -> list[Path]:
    """Delete vault summary files. Returns list of successfully deleted paths."""
    successfully_deleted: list[Path] = []
    for summary_path in deleted_summaries:
        try:
            summary_path.unlink()
            successfully_deleted.append(summary_path)
            logger.info("Deleted summary: %s", summary_path)
            ts = _utc_now()
            log_fn(f"{ts} | deletion | {summary_path.name}")
        except FileNotFoundError:
            # Already gone — treat as success (idempotent)
            successfully_deleted.append(summary_path)
            logger.debug("Summary already gone (FileNotFoundError): %s", summary_path)
        except OSError as exc:
            logger.error("Failed to delete summary %s: %s", summary_path, exc)
    return successfully_deleted


def _collect_remaining_summaries(
    vault_root: Path,
    excluded_vault_paths: set[Path],
) -> list[Path]:
    """Return all .md summary files in the vault that were not deleted this run."""
    remaining: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(vault_root):
        dirnames[:] = [d for d in dirnames if d not in VAULT_EXCLUDED_DIRS]

        current_dir = Path(dirpath)
        for filename in filenames:
            if not filename.endswith(".md"):
                continue
            if current_dir == vault_root and filename in VAULT_SPECIAL_FILES:
                continue
            if filename == "overview.md":
                continue

            full_path = current_dir / filename
            if full_path in excluded_vault_paths:
                continue

            remaining.append(full_path)

    return remaining


def _build_dead_wikilinks(
    deleted_vault_paths: list[Path],
    vault_root: Path,
) -> set[str]:
    """Build the set of wikilink strings for deleted summaries.

    Example: if vault/src/auth/login.py.md was deleted,
    the dead wikilink is '[[src/auth/login.py]]'.
    """
    dead: set[str] = set()
    for summary_path in deleted_vault_paths:
        link = wikilink(summary_path, vault_root)
        dead.add(link)
    return dead


def _remove_backlinks_from_file(
    summary_path: Path,
    dead_links: set[str],
    log_fn: Callable[[str], None],
    logger: logging.Logger,
) -> int:
    """Remove dead backlink lines from a summary file.

    Only removes lines inside the ## References section that contain a dead wikilink.
    Returns the number of backlink lines removed.
    """
    try:
        content = summary_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Cannot read %s for backlink cleanup: %s", summary_path, exc)
        return 0

    lines = content.splitlines(keepends=True)
    new_lines: list[str] = []
    in_references_section = False
    removed_count = 0

    for line in lines:
        stripped = line.rstrip("\n").rstrip()

        # Track entry into ## References section
        if stripped == "## References":
            in_references_section = True
            new_lines.append(line)
            continue

        # Exit ## References on any new ## heading
        if in_references_section and stripped.startswith("## ") and stripped != "## References":
            in_references_section = False

        # Within References: check if this line contains a dead wikilink
        if in_references_section:
            is_dead = any(dead_link in stripped for dead_link in dead_links)
            if is_dead:
                removed_count += 1
                logger.debug(
                    "Removing dead backlink in %s: %r", summary_path.name, stripped
                )
                # Do not append this line — it is dropped
                continue

        new_lines.append(line)

    if removed_count == 0:
        return 0

    # Rewrite the file without the dead backlink lines
    try:
        summary_path.write_text("".join(new_lines), encoding="utf-8")
        ts = _utc_now()
        log_fn(f"{ts} | backlink-removed | {summary_path.name} ({removed_count} link(s) removed)")
        logger.info(
            "Cleaned %d dead backlink(s) from %s", removed_count, summary_path.name
        )
    except OSError as exc:
        logger.error("Cannot rewrite %s after backlink cleanup: %s", summary_path, exc)
        return 0  # Report as zero since cleanup did not persist

    return removed_count


def _cleanup_empty_directories(
    vault_root: Path,
    logger: logging.Logger,
) -> list[Path]:
    """Remove empty vault directories created by summary deletion.

    Walks bottom-up so that a directory emptied by removing its last child
    subdirectory is itself eligible for removal in the same pass.

    Returns list of directories removed.
    """
    removed: list[Path] = []

    # os.walk with topdown=False visits children before parents (bottom-up)
    for dirpath, dirnames, filenames in os.walk(vault_root, topdown=False):
        current_dir = Path(dirpath)

        # Never remove the vault root itself
        if current_dir == vault_root:
            continue

        # Never remove logs/ or queries/ even if empty
        if current_dir.name in VAULT_EXCLUDED_DIRS:
            continue

        # Check if the directory is now empty (no files, no subdirectories)
        try:
            entries = list(current_dir.iterdir())
        except OSError as exc:
            logger.warning("Cannot inspect directory %s: %s", current_dir, exc)
            continue

        if not entries:
            try:
                current_dir.rmdir()
                removed.append(current_dir)
                logger.info("Removed empty vault directory: %s", current_dir)
            except OSError as exc:
                logger.warning("Cannot remove empty directory %s: %s", current_dir, exc)

    return removed


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def apply_deletions(
    change_set: ChangeSet,
    vault_root: Path,
    log_fn: Callable[[str], None],
    logger: logging.Logger,
) -> DeletionResult:
    """Orchestrate Phase 2 deletions: delete summaries, clean backlinks, prune dirs.

    Parameters
    ----------
    change_set:
        Phase 1 result from scan_codebase(); change_set.deleted_summaries is the
        list of vault summary paths to delete.
    vault_root:
        Absolute Path to the vault root directory.
    log_fn:
        Callable that appends one entry to log.md (e.g.
        ``lambda entry: append_log_md(vault_root, entry)``); the caller
        constructs this closure.
    logger:
        Application-wide logger (from setup_logging()), used for DEBUG/INFO
        events to the operational debug log.

    Returns
    -------
    DeletionResult
        Populated with per-step outcomes.
    """
    result = DeletionResult()

    if not change_set.deleted_summaries:
        logger.debug("No deletions in change_set; skipping deletion phase")
        return result

    # Step 1: Delete summary files
    deleted_paths = _delete_summary_files(
        change_set.deleted_summaries, log_fn, logger
    )
    result.deleted_files = deleted_paths
    result.failed_deletions = [
        p for p in change_set.deleted_summaries if p not in set(deleted_paths)
    ]

    if not deleted_paths:
        # All deletions failed; no backlink cleanup needed
        logger.warning(
            "All %d deletions failed; skipping backlink cleanup",
            len(change_set.deleted_summaries),
        )
        return result

    # Step 2: Collect remaining summaries (exclude deleted paths from scan)
    deleted_set = set(deleted_paths)
    remaining = _collect_remaining_summaries(vault_root, deleted_set)
    logger.debug("Backlink scan: %d remaining summary files to check", len(remaining))

    # Step 3: Build set of dead wikilink strings
    dead_links = _build_dead_wikilinks(deleted_paths, vault_root)
    logger.debug("Dead wikilinks to remove: %s", dead_links)

    # Step 4: Remove dead backlinks from remaining summaries
    for summary_path in remaining:
        count = _remove_backlinks_from_file(summary_path, dead_links, log_fn, logger)
        if count > 0:
            result.backlinks_cleaned.append((summary_path, count))

    # Step 5: Clean up empty directories
    result.removed_dirs = _cleanup_empty_directories(vault_root, logger)

    logger.info(
        "Deletion phase complete: deleted=%d failed=%d files_with_backlinks_cleaned=%d empty_dirs_removed=%d",
        len(result.deleted_files),
        len(result.failed_deletions),
        len(result.backlinks_cleaned),
        len(result.removed_dirs),
    )
    return result
