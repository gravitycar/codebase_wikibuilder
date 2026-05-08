"""Index writer for Codebase Wiki Builder.

Provides rebuild_index(), which completely rewrites index.md as a two-column
markdown table covering every current wiki page: source-file summaries,
overview.md files, and query pages under queries/.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from codebase_wiki_builder.vault import VAULT_EXCLUDED_DIRS, VAULT_SPECIAL_FILES, wikilink

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

INDEX_FILENAME = "index.md"

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_TABLE_ROW_RE = re.compile(r"^\|\s*(\[\[.*?\]\])\s*\|\s*(.*?)\s*\|$")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def rebuild_index(vault_root: Path, logger: logging.Logger) -> None:
    """Completely rewrite index.md as a two-column markdown table.

    Covers:
    - All source-file summary pages (vault-mirrored .md files)
    - All overview.md files (root and subdirectory)
    - All query pages under queries/
    """
    # Step 1: Read old index to carry forward query descriptions and stale annotations
    old_descriptions = parse_existing_index(vault_root)

    # Step 2: Collect all wiki pages
    pages = _collect_summary_pages(vault_root)       # source summaries
    overviews = _collect_overview_pages(vault_root)  # overview.md files
    query_pages = _collect_query_pages(vault_root)   # queries/*.md

    # Step 3: Build table rows
    rows: list[tuple[str, str]] = []
    for page_path in sorted(pages):
        link = wikilink(page_path, vault_root)
        old_desc = old_descriptions.get(_wikilink_target(link))
        desc = (old_desc if old_desc and old_desc != "(no description)" else None) or _extract_description(page_path)
        rows.append((link, desc))

    for page_path in sorted(overviews):
        link = wikilink(page_path, vault_root)
        old_desc = old_descriptions.get(_wikilink_target(link))
        desc = (old_desc if old_desc and old_desc != "(no description)" else None) or _overview_description(page_path, vault_root)
        rows.append((link, desc))

    for page_path in sorted(query_pages):
        link = wikilink(page_path, vault_root)
        old_desc = old_descriptions.get(_wikilink_target(link))
        desc = (old_desc if old_desc and old_desc != "(no description)" else None) or _extract_description(page_path)
        rows.append((link, desc))

    # Step 4: Write index.md
    _write_index(vault_root, rows, logger)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def parse_existing_index(vault_root: Path) -> dict[str, str]:
    """Read existing index.md and return a wikilink_target → description mapping.

    Returns empty dict if index.md does not exist or has no table.
    """
    index_path = vault_root / INDEX_FILENAME
    if not index_path.exists():
        return {}
    try:
        content = index_path.read_text(encoding="utf-8")
    except OSError:
        return {}

    result: dict[str, str] = {}
    for line in content.splitlines():
        m = _TABLE_ROW_RE.match(line.strip())
        if m:
            link_cell = m.group(1).strip()      # e.g. [[src/auth/login.py]]
            desc_cell = m.group(2).strip()       # e.g. "Handles login logic ⚠ stale"
            inner = _WIKILINK_RE.search(link_cell)
            if inner:
                result[inner.group(1)] = desc_cell
    return result


def _wikilink_target(link: str) -> str:
    """Extract the path inside a [[...]] string.

    Example: "[[src/auth/login.py]]" -> "src/auth/login.py"
    """
    m = _WIKILINK_RE.search(link)
    return m.group(1) if m else link


def _collect_summary_pages(vault_root: Path) -> list[Path]:
    """Walk the vault and return all source-file summary .md files.

    Applies the same exclusion rules as the scanner's vault walk:
    - Prunes VAULT_EXCLUDED_DIRS (logs/, queries/)
    - Excludes VAULT_SPECIAL_FILES at vault root
    - Excludes any file named overview.md
    - Only returns .md files
    """
    results: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(vault_root):
        # Prune excluded directories in-place
        dirnames[:] = [d for d in dirnames if d not in VAULT_EXCLUDED_DIRS]

        current_dir = Path(dirpath)
        for filename in filenames:
            if not filename.endswith(".md"):
                continue
            # Exclude special files at vault root
            if current_dir == vault_root and filename in VAULT_SPECIAL_FILES:
                continue
            # Exclude overview.md anywhere
            if filename == "overview.md":
                continue
            results.append(current_dir / filename)
    return results


def _collect_overview_pages(vault_root: Path) -> list[Path]:
    """Walk the vault and return all overview.md files.

    Returns root-level overview.md and any subdirectory overview.md files,
    excluding those under logs/ and queries/.
    """
    results: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(vault_root):
        # Prune excluded directories in-place
        dirnames[:] = [d for d in dirnames if d not in VAULT_EXCLUDED_DIRS]

        current_dir = Path(dirpath)
        for filename in filenames:
            if filename == "overview.md":
                results.append(current_dir / filename)
    return results


def _collect_query_pages(vault_root: Path) -> list[Path]:
    """Return all .md files directly under vault_root/queries/.

    Returns empty list if queries/ does not exist.
    """
    queries_dir = vault_root / "queries"
    if not queries_dir.is_dir():
        return []
    return [p for p in queries_dir.iterdir() if p.is_file() and p.suffix == ".md"]


def _extract_description(page_path: Path) -> str:
    """Extract a one-line description from a wiki page.

    Algorithm:
    1. Read all lines.
    2. Skip the first line if it starts with '# ' (the H1 title).
    3. Skip blank lines.
    4. Return the first non-blank, non-heading line, stripped, truncated to 120 chars.
    5. If no such line found, return "(no description)".
    """
    try:
        lines = page_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "(no description)"

    skipped_h1 = False
    for line in lines:
        stripped = line.strip()
        if not skipped_h1 and stripped.startswith("# "):
            skipped_h1 = True
            continue
        if not stripped:
            continue
        if stripped.startswith("#"):
            # Stop at ## References — nothing useful for descriptions past this point
            if stripped.lstrip("#").strip().lower() == "references":
                break
            # Skip other section headings (e.g., ## ClassName) — prose follows beneath
            continue
        return stripped[:120]
    return "(no description)"


def _overview_description(page_path: Path, vault_root: Path) -> str:
    """Return the appropriate description for an overview.md file.

    - Root overview.md: "Top-level application overview"
    - Subdirectory overview.md: "Directory overview: src/auth/"
    """
    if page_path.parent == vault_root:
        return "Top-level application overview"
    rel_dir = page_path.parent.relative_to(vault_root)
    return f"Directory overview: {rel_dir.as_posix()}/"


def _write_index(
    vault_root: Path,
    rows: list[tuple[str, str]],
    logger: logging.Logger,
) -> None:
    """Write the complete index.md as a two-column markdown table."""
    index_path = vault_root / INDEX_FILENAME
    lines = [
        "| File | Description |",
        "|------|-------------|",
    ]
    for link, desc in rows:
        # Escape pipe characters in description to avoid breaking table formatting
        safe_desc = desc.replace("|", "\\|")
        lines.append(f"| {link} | {safe_desc} |")

    content = "\n".join(lines) + "\n"
    index_path.write_text(content, encoding="utf-8")
    logger.info("index.md rebuilt with %d entries", len(rows))
