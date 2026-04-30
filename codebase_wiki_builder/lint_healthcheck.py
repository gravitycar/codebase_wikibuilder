"""Deep vault health-check for Codebase Wiki Builder lint command.

Implements run_health_check(), which batches all summary files through the
same tiktoken directory-subdivision algorithm used by the analysis command,
sends each batch to the LLM for four-category findings, synthesizes findings
across all batches, and writes lint-report.md.

Public API:
  - run_health_check(): main entry point

No typer imports — this is a pure logic module. The lint CLI (item 17) handles
all framework concerns.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from rich.console import Console

from codebase_wiki_builder.analysis import (
    ANALYSIS_CONTEXT_WINDOW,  # noqa: F401 — imported for reference; may be used in future checks
    build_batches,
    collect_summary_files,
)

if TYPE_CHECKING:
    from codebase_wiki_builder.analysis import AnalysisBatch
    from codebase_wiki_builder.lint_dedup import LintDedupResult
    from codebase_wiki_builder.llm_client import LLMClient

logger = logging.getLogger(__name__)
_console = Console()

# ---------------------------------------------------------------------------
# Prompt constants (documentation only — never use .format() on these)
# ---------------------------------------------------------------------------

# HEALTH_CHECK_BATCH_PROMPT is kept as documentation only — do NOT use with
# .format() at runtime. Use _build_batch_health_check_prompt() instead.
HEALTH_CHECK_BATCH_PROMPT = """\
You are performing a deep health-check of a codebase wiki. Below is the wiki's index (index.md) followed by a batch of summary files from one directory of the vault.

Analyze these pages and identify findings in EXACTLY FOUR categories:

1. ORPHAN PAGES: Query pages or summary pages with no inbound backlinks from any other wiki page.
2. MISSING CROSS-REFERENCES: Pairs of pages that are clearly related by their content but do not link to each other.
3. CONTRADICTIONS: Claims in one page that appear to contradict claims in another page (best-effort based on content).
4. CONCEPT GAPS: Important concepts mentioned across multiple pages but with no dedicated summary page.

For each finding, be specific: name the pages involved.
If you find nothing for a category in this batch, write "None found in this batch."

--- INDEX.MD ---
{index_content}
--- END INDEX ---

--- SUMMARY FILES ---
{combined_summaries}
--- END SUMMARY FILES ---

Format your response as:
## Orphan Pages
[findings or "None found in this batch."]

## Missing Cross-References
[findings or "None found in this batch."]

## Contradictions
[findings or "None found in this batch."]

## Concept Gaps
[findings or "None found in this batch."]
"""

# HEALTH_CHECK_SYNTHESIS_PROMPT is kept as documentation only — do NOT use with
# .format() at runtime. Use _build_health_check_synthesis_prompt() instead.
HEALTH_CHECK_SYNTHESIS_PROMPT = """\
You are synthesizing health-check findings from multiple batches of a codebase wiki analysis into a unified report. Each section below contains findings from one directory batch.

Produce a single unified report with EXACTLY FOUR sections. Deduplicate findings that appear in multiple batches. Consolidate related findings. If a category has no findings across all batches, write "None found."

--- PER-BATCH FINDINGS ---
{batch_findings}
--- END FINDINGS ---

Format your response as:
## Orphan Pages
[unified findings or "None found."]

## Missing Cross-References
[unified findings or "None found."]

## Contradictions
[unified findings or "None found."]

## Concept Gaps
[unified findings or "None found."]
"""

# LINT_REPORT_HEADER and DEDUP_SECTION_PLACEHOLDER are kept as documentation only.
LINT_REPORT_HEADER = """\
# Wiki Lint Report
Generated: {timestamp}

"""

DEDUP_SECTION_PLACEHOLDER = """\
## Deduplicated Query Pages
{dedup_entries}
"""


# ---------------------------------------------------------------------------
# Prompt builder functions (f-string based)
# ---------------------------------------------------------------------------

def _build_batch_health_check_prompt(index_content: str, combined_summaries: str) -> str:
    """Build the per-batch health-check prompt using an f-string.

    Uses an f-string rather than HEALTH_CHECK_BATCH_PROMPT.format() so that curly braces
    in untrusted content (index.md, vault summary files) cannot corrupt the prompt or raise
    KeyError at the Python layer.
    """
    return (
        "You are performing a deep health-check of a codebase wiki. Below is the wiki's "
        "index (index.md) followed by a batch of summary files from one directory of the vault.\n"
        "\n"
        "Analyze these pages and identify findings in EXACTLY FOUR categories:\n"
        "\n"
        "1. ORPHAN PAGES: Query pages or summary pages with no inbound backlinks from any other wiki page.\n"
        "2. MISSING CROSS-REFERENCES: Pairs of pages that are clearly related by their content but do not link to each other.\n"
        "3. CONTRADICTIONS: Claims in one page that appear to contradict claims in another page (best-effort based on content).\n"
        "4. CONCEPT GAPS: Important concepts mentioned across multiple pages but with no dedicated summary page.\n"
        "\n"
        "For each finding, be specific: name the pages involved.\n"
        'If you find nothing for a category in this batch, write "None found in this batch."\n'
        "\n"
        "--- INDEX.MD ---\n"
        f"{index_content}\n"
        "--- END INDEX ---\n"
        "\n"
        "--- SUMMARY FILES ---\n"
        f"{combined_summaries}\n"
        "--- END SUMMARY FILES ---\n"
        "\n"
        "Format your response as:\n"
        "## Orphan Pages\n"
        '[findings or "None found in this batch."]\n'
        "\n"
        "## Missing Cross-References\n"
        '[findings or "None found in this batch."]\n'
        "\n"
        "## Contradictions\n"
        '[findings or "None found in this batch."]\n'
        "\n"
        "## Concept Gaps\n"
        '[findings or "None found in this batch."]\n'
    )


def _build_health_check_synthesis_prompt(batch_findings_text: str) -> str:
    """Build the health-check synthesis prompt using an f-string.

    Uses an f-string rather than HEALTH_CHECK_SYNTHESIS_PROMPT.format() so that curly
    braces in untrusted LLM-generated per-batch findings text cannot corrupt the prompt
    or raise KeyError at the Python layer.
    """
    return (
        "You are synthesizing health-check findings from multiple batches of a codebase wiki "
        "analysis into a unified report. Each section below contains findings from one directory batch.\n"
        "\n"
        "Produce a single unified report with EXACTLY FOUR sections. Deduplicate findings that "
        "appear in multiple batches. Consolidate related findings. If a category has no findings "
        'across all batches, write "None found."\n'
        "\n"
        "--- PER-BATCH FINDINGS ---\n"
        f"{batch_findings_text}\n"
        "--- END FINDINGS ---\n"
        "\n"
        "Format your response as:\n"
        "## Orphan Pages\n"
        '[unified findings or "None found."]\n'
        "\n"
        "## Missing Cross-References\n"
        '[unified findings or "None found."]\n'
        "\n"
        "## Contradictions\n"
        '[unified findings or "None found."]\n'
        "\n"
        "## Concept Gaps\n"
        '[unified findings or "None found."]\n'
    )


# ---------------------------------------------------------------------------
# Step 1 — Read index.md
# ---------------------------------------------------------------------------

def _read_index_content(vault_root: Path, log: "logging.Logger") -> str:
    """Read index.md content for inclusion in every health-check batch."""
    index_path = vault_root / "index.md"
    try:
        return index_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("Cannot read index.md for health-check: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Step 4 — Per-batch health-check LLM call
# ---------------------------------------------------------------------------

def _run_batch_health_check(
    batch: "AnalysisBatch",
    index_content: str,
    llm_client: "LLMClient",
    log: "logging.Logger",
) -> str:
    """Send one batch of summary files to the LLM for health-check findings.

    Returns the LLM response text (four-category findings).
    Returns an empty string on LLM error.
    """
    combined = "\n\n---\n\n".join(
        f"File: {path.name}\n\n{content}"
        for path, content in zip(batch.file_paths, batch.contents)
    )
    prompt = _build_batch_health_check_prompt(index_content, combined)

    try:
        response = llm_client.complete(prompt)
        log.info(
            "Health-check batch for '%s': %d files processed",
            batch.vault_dir or "(root)", len(batch.file_paths),
        )
        return response
    except Exception as exc:
        log.error("LLM health-check batch failed for '%s': %s", batch.vault_dir, exc)
        return ""


# ---------------------------------------------------------------------------
# Step 5 — Synthesize findings
# ---------------------------------------------------------------------------

def _synthesize_health_check(
    batch_findings: list[tuple[str, str]],   # (vault_dir, findings_text)
    llm_client: "LLMClient",
    log: "logging.Logger",
) -> str:
    """Synthesize per-batch health-check findings into a unified report body.

    Returns the LLM response (four-section text).
    Falls back to concatenating all batch findings if LLM call fails.
    """
    combined_sections = "\n\n---\n\n".join(
        f"Directory: {vault_dir if vault_dir else '(root)'}\n\n{text}"
        for vault_dir, text in batch_findings
        if text.strip()
    )

    if not combined_sections:
        return (
            "## Orphan Pages\nNone found.\n\n"
            "## Missing Cross-References\nNone found.\n\n"
            "## Contradictions\nNone found.\n\n"
            "## Concept Gaps\nNone found."
        )

    prompt = _build_health_check_synthesis_prompt(combined_sections)

    try:
        return llm_client.complete(prompt)
    except Exception as exc:
        log.error("Health-check synthesis failed: %s", exc)
        # Fallback: concatenate batch findings as-is
        return combined_sections


# ---------------------------------------------------------------------------
# Step 6 — Write lint-report.md
# ---------------------------------------------------------------------------

def _write_lint_report(
    vault_root: Path,
    synthesis: str,
    dedup_entries: list[str],
    log: "logging.Logger",
) -> None:
    """Write lint-report.md to vault root. Overwrites on each run.

    Args:
        synthesis: The four-section synthesis text from the LLM.
        dedup_entries: List of 'old-page.md → merged-page.md' strings from dedup step.
                       Pass empty list if no deduplication was performed this run.
    """
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    dedup_content: str
    if dedup_entries:
        dedup_content = "\n".join(f"- {entry}" for entry in dedup_entries)
    else:
        dedup_content = "None"

    # Build report using f-strings (not .format() on template strings)
    header = f"# Wiki Lint Report\nGenerated: {timestamp}\n\n"
    dedup_section = f"## Deduplicated Query Pages\n{dedup_content}\n"

    report_content = (
        header
        + synthesis.strip()
        + "\n\n"
        + dedup_section
    )

    report_path = vault_root / "lint-report.md"
    try:
        report_path.write_text(report_content, encoding="utf-8")
        log.info("Wrote lint-report.md")
    except OSError as exc:
        log.error("Cannot write lint-report.md: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_health_check(
    vault_root: Path,
    llm_client: "LLMClient",
    log_fn: Callable[[str], None],
    dedup_result: "LintDedupResult | None" = None,
) -> None:
    """Run the deep vault health-check and write lint-report.md.

    Steps:
      1. Read index.md for structural context.
      2. Collect all summary files via collect_summary_files() from analysis.py.
      3. Build tiktoken batches via build_batches() from analysis.py.
      4. For each batch: send summary content + index.md to LLM for four-category findings.
      5. Synthesize per-batch findings into a unified report.
      6. Write lint-report.md.
      7. Print "Lint report written to lint-report.md".

    Args:
        vault_root: Absolute path to the vault root directory.
        llm_client: Configured LLM client.
        log_fn: Callable that accepts a pre-formatted log entry string.
        dedup_result: Optional result from deduplicate_query_pages() (Part 2).
                      If provided, merged pages are listed in ## Deduplicated Query Pages.
    """
    log = logging.getLogger(__name__)

    # Step 1: Read index.md for batch context
    index_content = _read_index_content(vault_root, log)

    # Step 2: Collect summary files
    summary_files = collect_summary_files(vault_root)
    log.info("Health-check: found %d summary files", len(summary_files))

    # If no summary files, write a minimal report
    if not summary_files:
        log.warning("No summary files found for health-check")
        _write_lint_report(
            vault_root,
            (
                "## Orphan Pages\nNone found.\n\n"
                "## Missing Cross-References\nNone found.\n\n"
                "## Contradictions\nNone found.\n\n"
                "## Concept Gaps\nNone found."
            ),
            dedup_entries=[],
            log=log,
        )
        _console.print("Lint report written to lint-report.md")
        return

    # Step 3: Build tiktoken batches (identical to analysis command)
    batches = build_batches(summary_files, vault_root, log)
    log.info("Health-check: built %d batch(es)", len(batches))

    # Step 4: Per-batch health-check
    batch_findings: list[tuple[str, str]] = []
    for batch in batches:
        findings_text = _run_batch_health_check(batch, index_content, llm_client, log)
        batch_findings.append((batch.vault_dir, findings_text))

    # Step 5: Synthesize findings
    synthesis = _synthesize_health_check(batch_findings, llm_client, log)

    # Prepare dedup entries for the report
    dedup_entries: list[str] = []
    if dedup_result:
        for surviving_path, deleted_paths in dedup_result.merged_groups:
            surviving_rel = surviving_path.relative_to(vault_root).as_posix()
            for deleted_path in deleted_paths:
                deleted_rel = deleted_path.relative_to(vault_root).as_posix()
                dedup_entries.append(f"{deleted_rel} → {surviving_rel}")

    # Step 6: Write lint-report.md
    _write_lint_report(vault_root, synthesis, dedup_entries, log)

    _console.print("Lint report written to lint-report.md")
