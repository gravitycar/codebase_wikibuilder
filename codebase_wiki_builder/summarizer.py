"""Summarization and summary file writer for Codebase Wiki Builder.

Implements Ingest Phase 2 core: for every source file classified as new or
modified by the Phase 1 scanner, summarize_file() constructs and sends the
LLM prompt, parses the structured response, validates returned file references
against the real codebase file tree, and assembles the final summary string.
write_summary() writes that string to the correct vault path, creating parent
directories as needed.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from codebase_wiki_builder.config import WikiConfig
from codebase_wiki_builder.llm_client import LLMClient, LLMError  # noqa: F401 (re-exported)
from codebase_wiki_builder.vault import (
    compute_md5,
    md5_footer,
    vault_path_for_source,
    wikilink,
)

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JSON fence regex
# ---------------------------------------------------------------------------

_JSON_FENCE_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)

# ---------------------------------------------------------------------------
# PROMPT_TEMPLATE — documentation only, NOT used with .format() at runtime.
#
# Curly braces in file_content (untrusted input) would corrupt the template
# or raise KeyError if this were used with str.format().  Use _build_prompt()
# which constructs the prompt via concatenated string literals and f-strings.
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = """\
You are analyzing a source file to produce a wiki summary. Your response MUST be a JSON object
enclosed in a ```json ... ``` code fence. Do not include any text outside the fence.

## File to summarize
Path (relative to codebase root): {relative_path}

## File contents
```
{file_content}
```

## Instructions

Produce a JSON object with exactly these fields:

1. "description": A markdown string summarizing what this file does.
   - If the file defines one or more classes or modules, produce a sub-section (### ClassName)
     for each, briefly listing its key properties and methods.
   - Otherwise, write 1-3 paragraphs of plain prose describing the file's purpose.

2. "explicit_references": A JSON array of relative file paths (strings).
   List files IN THE CODEBASE that explicitly import, require, or include THIS file
   (not files that this file imports from). Use paths relative to the codebase root.
   If none, return an empty array [].

3. "dynamic_references": A JSON array of objects, each with "path" and "reason" fields.
   List files IN THE CODEBASE that likely reference THIS file at runtime through dynamic
   patterns (e.g., dynamic imports, plugin loaders, string-based path construction).
   If none, return an empty array [].

Return ONLY the JSON object inside a ```json ... ``` fence. No other text.
"""


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _build_prompt(
    source_file: Path,
    codebase_root: Path,
    file_content: str,
) -> str:
    """Construct the LLM summarization prompt for a single source file.

    Uses concatenated string literals with f-string interpolation for
    relative_path and file_content — never str.format() — so that curly
    braces in untrusted content cannot corrupt the prompt or raise KeyError.
    """
    relative_path = source_file.relative_to(codebase_root).as_posix()
    return (
        "You are analyzing a source file to produce a wiki summary. Your response MUST be a JSON object\n"
        "enclosed in a ```json ... ``` code fence. Do not include any text outside the fence.\n"
        "\n"
        "## File to summarize\n"
        f"Path (relative to codebase root): {relative_path}\n"
        "\n"
        "## File contents\n"
        "```\n"
        f"{file_content}\n"
        "```\n"
        "\n"
        "## Instructions\n"
        "\n"
        'Produce a JSON object with exactly these fields:\n'
        "\n"
        '1. "description": A markdown string summarizing what this file does.\n'
        "   - If the file defines one or more classes or modules, produce a sub-section (### ClassName)\n"
        "     for each, briefly listing its key properties and methods.\n"
        "   - Otherwise, write 1-3 paragraphs of plain prose describing the file's purpose.\n"
        "\n"
        '2. "explicit_references": A JSON array of relative file paths (strings).\n'
        "   List files IN THE CODEBASE that explicitly import, require, or include THIS file\n"
        "   (not files that this file imports from). Use paths relative to the codebase root.\n"
        "   If none, return an empty array [].\n"
        "\n"
        '3. "dynamic_references": A JSON array of objects, each with "path" and "reason" fields.\n'
        "   List files IN THE CODEBASE that likely reference THIS file at runtime through dynamic\n"
        "   patterns (e.g., dynamic imports, plugin loaders, string-based path construction).\n"
        "   If none, return an empty array [].\n"
        "\n"
        "Return ONLY the JSON object inside a ```json ... ``` fence. No other text.\n"
    )


# ---------------------------------------------------------------------------
# LLM response parsing
# ---------------------------------------------------------------------------

def _parse_llm_response(raw: str, logger: logging.Logger) -> dict:
    """Extract and parse the JSON object from the LLM response.

    Returns a dict with keys:
      - description (str)
      - explicit_references (list[str])
      - dynamic_references (list[dict] with 'path' and 'reason' string fields)

    Falls back to a dict with the raw text as description and empty lists on
    any parse failure (missing fence, bad JSON, missing/empty description).
    Failures are logged at WARNING level.
    """
    m = _JSON_FENCE_RE.search(raw)
    if not m:
        logger.warning(
            "LLM response contained no ```json``` fence; using raw text as description"
        )
        return {"description": raw.strip(), "explicit_references": [], "dynamic_references": []}

    json_text = m.group(1)
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as exc:
        logger.warning(
            "LLM response JSON parse failed (%s); using raw text as description", exc
        )
        return {"description": raw.strip(), "explicit_references": [], "dynamic_references": []}

    # Validate and coerce fields
    description = str(data.get("description", "")).strip()
    if not description:
        description = raw.strip()
        logger.warning("LLM response 'description' field is empty; using raw text")

    explicit_refs = data.get("explicit_references", [])
    if not isinstance(explicit_refs, list):
        logger.warning("'explicit_references' is not a list; ignoring")
        explicit_refs = []
    explicit_refs = [r for r in explicit_refs if isinstance(r, str) and r.strip()]

    dynamic_refs = data.get("dynamic_references", [])
    if not isinstance(dynamic_refs, list):
        logger.warning("'dynamic_references' is not a list; ignoring")
        dynamic_refs = []

    validated_dynamic: list[dict] = []
    for item in dynamic_refs:
        if (
            isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and isinstance(item.get("reason"), str)
        ):
            if item["path"].strip():
                validated_dynamic.append(
                    {"path": item["path"].strip(), "reason": item["reason"].strip()}
                )
        else:
            logger.warning("Skipping malformed dynamic_references entry: %r", item)

    return {
        "description": description,
        "explicit_references": explicit_refs,
        "dynamic_references": validated_dynamic,
    }


# ---------------------------------------------------------------------------
# Reference validation
# ---------------------------------------------------------------------------

def _validate_references(
    explicit_refs: list[str],
    dynamic_refs: list[dict],
    codebase_root: Path,
    logger: logging.Logger,
) -> tuple[list[Path], list[dict]]:
    """Resolve reference paths against the real codebase. Discard non-existent paths.

    lstrip("/") on LLM-returned paths prevents absolute-path injection (e.g.,
    "/etc/passwd" resolving to an absolute path via Path.__truediv__).

    Returns
    -------
    valid_explicit : list[Path]
        Absolute paths of valid explicit references.
    valid_dynamic : list[dict]
        Dicts with keys 'path' (Path) and 'reason' (str) for valid dynamic refs.
    """
    valid_explicit: list[Path] = []
    for ref_str in explicit_refs:
        ref_path = codebase_root / ref_str.lstrip("/")
        if ref_path.exists() and ref_path.is_file():
            valid_explicit.append(ref_path)
        else:
            logger.debug("Discarding explicit reference (not found): %s", ref_str)

    valid_dynamic: list[dict] = []
    for item in dynamic_refs:
        ref_path = codebase_root / item["path"].lstrip("/")
        if ref_path.exists() and ref_path.is_file():
            valid_dynamic.append({"path": ref_path, "reason": item["reason"]})
        else:
            logger.debug("Discarding dynamic reference (not found): %s", item["path"])

    return valid_explicit, valid_dynamic


# ---------------------------------------------------------------------------
# Summary string assembly
# ---------------------------------------------------------------------------

def _assemble_summary(
    source_file: Path,
    codebase_root: Path,
    vault_root: Path,
    description: str,
    valid_explicit: list[Path],
    valid_dynamic: list[dict],
    md5_hex: str,
) -> str:
    """Assemble the complete summary markdown string.

    Format (FR-3.5):
        # <relative path from codebase root>

        <description>

        ## References
        - [[vault/relative/path/to/explicit/file]]
        - [[vault/relative/path/to/dynamic/file]] (inferred)
        - ...

        <!-- md5: <hexdigest> -->

    The ``## References`` section is always emitted, even if both reference
    lists are empty — consistent format for downstream processing.
    Dynamic references are annotated with ``(inferred)``; explicit refs are not.
    The MD5 footer is the last line of the file.
    """
    relative_path = source_file.relative_to(codebase_root).as_posix()
    lines: list[str] = []

    # H1 title
    lines.append(f"# {relative_path}")
    lines.append("")

    # Description block
    lines.append(description.strip())
    lines.append("")

    # ## References section (always emitted)
    lines.append("## References")
    for explicit_path in valid_explicit:
        vault_summary = vault_path_for_source(explicit_path, codebase_root, vault_root)
        link = wikilink(vault_summary, vault_root)
        lines.append(f"- {link}")
    for dyn_item in valid_dynamic:
        vault_summary = vault_path_for_source(dyn_item["path"], codebase_root, vault_root)
        link = wikilink(vault_summary, vault_root)
        lines.append(f"- {link} (inferred)")

    # MD5 footer as the last line
    lines.append("")
    lines.append(md5_footer(md5_hex))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def summarize_file(
    path: Path,
    llm_client: LLMClient,
    config: WikiConfig,
    vault_root: Path,
    logger: logging.Logger,
) -> str:
    """Summarize a single source file.

    Reads file content, builds prompt, calls LLM, parses structured response,
    validates references against the real codebase, assembles and returns the
    final summary markdown string.

    The returned string is ready to be passed to write_summary().

    Parameters
    ----------
    path : Path
        Absolute path to the source file to summarize.
    llm_client : LLMClient
        Configured LLM client (handles inter-request delay and retries).
    config : WikiConfig
        Application configuration (provides codebase_path).
    vault_root : Path
        Absolute path to the vault root directory.
    logger : logging.Logger
        Logger for debug/warning messages.

    Raises
    ------
    LLMError
        If the LLM API call fails (non-retriable or all retries exhausted).
    OSError
        If the source file cannot be read.
    """
    codebase_root = Path(config.codebase_path)

    # Read file content — errors="replace" avoids crashing on non-UTF-8 bytes
    file_content = path.read_text(encoding="utf-8", errors="replace")

    # Compute MD5 BEFORE the LLM call so the footer reflects the file as sent
    md5_hex = compute_md5(path)

    # Build the prompt
    prompt = _build_prompt(path, codebase_root, file_content)

    # Call LLM (LLMClient enforces inter-request delay internally)
    logger.debug("Summarizing: %s", path)
    raw_response = llm_client.complete(prompt)

    # Parse structured response
    parsed = _parse_llm_response(raw_response, logger)

    # Validate references against real codebase file tree
    valid_explicit, valid_dynamic = _validate_references(
        parsed["explicit_references"],
        parsed["dynamic_references"],
        codebase_root,
        logger,
    )

    # Assemble final summary markdown
    summary_str = _assemble_summary(
        path,
        codebase_root,
        vault_root,
        parsed["description"],
        valid_explicit,
        valid_dynamic,
        md5_hex,
    )

    logger.debug(
        "Summary assembled for %s: %d explicit refs, %d dynamic refs",
        path.name,
        len(valid_explicit),
        len(valid_dynamic),
    )
    return summary_str


def write_summary(vault_summary_path: Path, summary_str: str) -> None:
    """Write a summary string to the vault at the given path.

    Creates parent directories if they do not exist.
    Overwrites any existing summary at that path (correct for re-summarization).

    Parameters
    ----------
    vault_summary_path : Path
        Absolute path where the summary file should be written.
    summary_str : str
        The complete summary markdown string to write.

    Raises
    ------
    OSError
        If the file or its parent directories cannot be created/written
        (permission error, disk full, etc.).
    """
    vault_summary_path.parent.mkdir(parents=True, exist_ok=True)
    vault_summary_path.write_text(summary_str, encoding="utf-8")
