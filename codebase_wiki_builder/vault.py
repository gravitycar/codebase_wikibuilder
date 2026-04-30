"""Vault file utilities for Codebase Wiki Builder.

Provides path mirroring, slug generation, MD5 hashing, wikilink formatting,
binary-file detection, and excluded-directory constants used throughout the
application.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

EXCLUDED_DIRS: frozenset[str] = frozenset({
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".maps",
})

BINARY_EXTENSIONS: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg",
    ".pdf", ".zip", ".tar", ".gz", ".exe", ".dll", ".so",
    ".pyc", ".class", ".wasm",
})

VAULT_SPECIAL_FILES: frozenset[str] = frozenset({
    "index.md", "log.md", "overview.md", "lint-report.md"
})

VAULT_EXCLUDED_DIRS: frozenset[str] = frozenset({"logs", "queries"})

# Regex for the MD5 footer comment at the end of summary files
MD5_FOOTER_RE = re.compile(r"<!--\s*md5:\s*([a-f0-9]{32})\s*-->")


# ---------------------------------------------------------------------------
# Binary file detection
# ---------------------------------------------------------------------------

def is_binary_file(path: Path) -> bool:
    """Return True if the file should be excluded as binary.

    A file is considered binary when:
    1. Its suffix (lowercased) is in BINARY_EXTENSIONS, OR
    2. It contains null bytes (\\x00) in its first 8,192 bytes, OR
    3. Its first 8,192 bytes cannot be decoded as UTF-8.

    OSError (unreadable files) is caught and treated as binary (skip them).
    """
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return True
    try:
        chunk = path.read_bytes()[:8192]
        if b"\x00" in chunk:
            return True
        chunk.decode("utf-8")
        return False
    except (OSError, UnicodeDecodeError):
        return True


# ---------------------------------------------------------------------------
# Summary filename and path utilities
# ---------------------------------------------------------------------------

def summary_filename(source_file: Path) -> str:
    """Return the summary filename for a given source file.

    Appends `.md` to the full filename (including any existing extension):
      user_service.py  ->  user_service.py.md
      Makefile         ->  Makefile.md
      .env             ->  .env.md
    """
    return source_file.name + ".md"


def mirror_path(source_file: Path, codebase_root: Path, vault_root: Path) -> Path:
    """Return the vault directory that mirrors the source file's parent directory.

    Does NOT append the filename — use vault_path_for_source() for the full
    summary path.

    Raises ValueError (from Path.relative_to) if source_file is not under
    codebase_root.
    """
    relative_dir = source_file.parent.relative_to(codebase_root)
    return vault_root / relative_dir


def vault_path_for_source(
    source_file: Path,
    codebase_root: Path,
    vault_root: Path,
) -> Path:
    """Return the full path of the summary file in the vault.

    Example:
      codebase_root=/home/user/myapp
      source_file=/home/user/myapp/src/auth/login.py
      vault_root=/home/user/vault
      -> /home/user/vault/src/auth/login.py.md
    """
    return mirror_path(source_file, codebase_root, vault_root) / summary_filename(source_file)


def source_path_from_vault(
    summary_file: Path,
    vault_root: Path,
    codebase_root: Path,
) -> Path:
    """Return the source file path corresponding to a vault summary file.

    Reverse mapping of vault_path_for_source(). Used when enumerating existing
    summaries to detect deletions.

    Assumes summary_file.name ends in `.md` (invariant from summary_filename()).
    """
    # summary_file.name is like "login.py.md"; strip trailing ".md"
    source_name = summary_file.name[:-3]  # remove ".md"
    relative_dir = summary_file.parent.relative_to(vault_root)
    return codebase_root / relative_dir / source_name


# ---------------------------------------------------------------------------
# Wikilink formatting
# ---------------------------------------------------------------------------

def wikilink(vault_summary_path: Path, vault_root: Path) -> str:
    """Return an Obsidian wikilink string for a summary file.

    The path is relative to the vault root, and the .md extension is omitted
    (Obsidian convention).

    Example:
      vault_summary_path=/vault/src/auth/login.py.md, vault_root=/vault
      -> [[src/auth/login.py]]
    """
    relative = vault_summary_path.relative_to(vault_root)
    # Remove .md extension: Obsidian wikilinks omit the extension
    without_ext = relative.with_suffix("") if relative.suffix == ".md" else relative
    return f"[[{without_ext.as_posix()}]]"


# ---------------------------------------------------------------------------
# Slug generation
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    """Convert a natural-language string into a URL-safe filename slug.

    Lowercases, replaces spaces with hyphens, strips non-alphanumeric/
    non-hyphen characters, collapses multiple hyphens, and strips leading/
    trailing hyphens.

    Example: "How does auth work?" -> "how-does-auth-work"

    May return an empty string if the entire input consists of non-alphanumeric
    characters — callers must handle this edge case.
    """
    slug = text.lower()
    slug = slug.replace(" ", "-")
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug)   # collapse multiple hyphens
    slug = slug.strip("-")               # strip leading/trailing hyphens
    return slug


# ---------------------------------------------------------------------------
# MD5 hashing
# ---------------------------------------------------------------------------

def compute_md5(path: Path) -> str:
    """Compute the MD5 hash of a file's full contents.

    Returns a 32-character lowercase hexdigest. Reads in 64 KiB chunks to
    handle large files without loading them entirely into memory.
    """
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_stored_md5(summary_path: Path) -> str | None:
    """Read the MD5 footer comment from the last line of a summary file.

    Returns the 32-character hexdigest if found, or None if the file does not
    exist, is unreadable, or has no valid footer.
    """
    if not summary_path.exists():
        return None
    try:
        text = summary_path.read_text(encoding="utf-8")
    except OSError:
        return None
    last_line = text.rstrip("\n").rsplit("\n", 1)[-1]
    m = MD5_FOOTER_RE.search(last_line)
    return m.group(1) if m else None


def md5_footer(hexdigest: str) -> str:
    """Return the formatted MD5 footer comment string.

    Example: md5_footer("abc...") -> "<!-- md5: abc... -->"
    """
    return f"<!-- md5: {hexdigest} -->"
