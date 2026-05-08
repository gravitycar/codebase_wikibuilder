"""CLI entry point for Codebase Wiki Builder.

Defines the Typer application and the `ingest`, `analysis`, `query`, `lint`,
and `help` subcommands.

All core module imports (scanner, summarizer, deletion, index_writer,
staleness, llm_client, query_engine, query_persistence, lint_staleness,
lint_dedup, lint_healthcheck) are deferred to function bodies to improve
startup time and avoid circular import risks.
Only typer and rich are imported at module level because `app` must be
importable at module level.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Optional

import typer
from rich.console import Console

if TYPE_CHECKING:
    from codebase_wiki_builder.config import WikiConfig
    from codebase_wiki_builder.llm_client import LLMClient
    from codebase_wiki_builder.lint_dedup import LintDedupResult
    from codebase_wiki_builder.lint_staleness import LintStalenessResult
    from codebase_wiki_builder.scanner import ChangeSet

# ---------------------------------------------------------------------------
# Typer application
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="codewiki",
    help="Codebase Wiki Builder — manage your Obsidian wiki.",
    add_completion=False,
    no_args_is_help=True,
)


# ---------------------------------------------------------------------------
# ingest subcommand
# ---------------------------------------------------------------------------

@app.command()
def ingest(
    vault_path: Path = typer.Option(
        Path("."),
        "--vault",
        "-v",
        help="Path to the Obsidian vault root (default: current directory).",
        exists=False,   # validated manually for better error messages
    ),
) -> None:
    """Scan the target codebase and update wiki summaries."""
    from codebase_wiki_builder.logging_setup import setup_logging, append_log_md

    vault_root = vault_path.resolve()

    # Vault root guard — must exist before we attempt to create logs/ under it
    if not vault_root.is_dir():
        typer.echo(f"Error: vault directory does not exist: {vault_root}", err=True)
        raise typer.Exit(code=1)

    # Step 1: Setup logging
    logger = setup_logging(vault_root)
    log_fn: Callable[[str], None] = lambda entry: append_log_md(vault_root, entry)

    # Step 2: Load or create config (first-run prompt if missing)
    config = _load_or_prompt_config(vault_root, logger)

    # Step 3: Phase 1 — compute change-set (no vault writes)
    change_set = _run_phase1(config, vault_root, logger)

    # Step 4: Phase 2 — apply changes
    failed_files = _run_phase2(change_set, config, vault_root, log_fn, logger)

    # Step 5: Print completion summary and exit
    _print_summary(change_set, failed_files, vault_root)
    _exit_with_code(failed_files)


# ---------------------------------------------------------------------------
# Helper: config loading
# ---------------------------------------------------------------------------

def _load_or_prompt_config(vault_root: Path, logger: logging.Logger) -> WikiConfig:
    """Load config from vault_root, or prompt for first-run setup if missing.

    The `ingest` command is the only command that handles the missing-config
    case by prompting. Other commands call load_config() directly (which
    exits with code 1 if the file is missing).
    """
    from codebase_wiki_builder.config import (
        CONFIG_FILENAME,
        load_config,
        prompt_for_config,
    )

    config_path = vault_root / CONFIG_FILENAME
    if not config_path.exists():
        logger.info(
            "No config file found at %s; prompting for first-run setup", config_path
        )
        config = prompt_for_config(vault_root)
    else:
        config = load_config(vault_root)  # exits with code 1 if invalid

    logger.info(
        "Config loaded: codebase=%s provider=%s model=%s",
        config.codebase_path,
        config.llm_provider,
        config.llm_model,
    )
    return config


# ---------------------------------------------------------------------------
# Helper: Phase 1
# ---------------------------------------------------------------------------

def _run_phase1(
    config: WikiConfig,
    vault_root: Path,
    logger: logging.Logger,
) -> ChangeSet:
    """Run Phase 1: scan codebase and compute the change-set.

    Makes no vault changes — only reads and computes.
    """
    from codebase_wiki_builder.scanner import scan_codebase

    console = Console()
    console.print("[bold]Phase 1:[/bold] Scanning codebase for changes…")

    change_set = scan_codebase(config, vault_root, logger)

    console.print(
        f"  Found: [green]{len(change_set.new_files)} new[/green], "
        f"[yellow]{len(change_set.modified_files)} modified[/yellow], "
        f"[red]{len(change_set.deleted_summaries)} deleted[/red], "
        f"{len(change_set.skipped_unchanged)} unchanged"
    )
    return change_set


# ---------------------------------------------------------------------------
# Helper: Phase 2
# ---------------------------------------------------------------------------

def _run_phase2(
    change_set: ChangeSet,
    config: WikiConfig,
    vault_root: Path,
    log_fn: Callable[[str], None],
    logger: logging.Logger,
) -> list[Path]:
    """Run Phase 2: apply all changes from the Phase 1 change-set.

    Sub-steps:
      A — Summarize new/modified files
      B — Apply deletions
      C — Rebuild index.md
      D — Staleness detection

    Returns
    -------
    list[Path]
        Source file paths that failed to be summarized (non-fatal OSError).
        A non-empty list results in exit code 2.

    Raises
    ------
    SystemExit(1)
        On a fatal LLMError (rate-limit exhaustion or non-retriable API error).
    """
    from codebase_wiki_builder.llm_client import LLMClient, LLMError
    from codebase_wiki_builder.summarizer import summarize_file, write_summary
    from codebase_wiki_builder.deletion import apply_deletions
    from codebase_wiki_builder.index_writer import rebuild_index
    from codebase_wiki_builder.staleness import detect_stale_queries
    from codebase_wiki_builder.vault import vault_path_for_source
    from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn

    console = Console()
    codebase_root = Path(config.codebase_path)
    failed_files: list[Path] = []

    # Build LLM client (instantiated once; manages inter-request delay internally)
    llm_client = LLMClient(config)

    # --- Sub-step A: Summarization ---
    files_to_summarize = change_set.new_files + change_set.modified_files
    if files_to_summarize:
        console.print(
            f"\n[bold]Phase 2a:[/bold] Summarizing {len(files_to_summarize)} file(s)…"
        )
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Summarizing…", total=len(files_to_summarize))
            for source_file in files_to_summarize:
                progress.update(task, description=f"[cyan]{source_file.name}[/cyan]")
                try:
                    summary_str = summarize_file(
                        source_file, llm_client, config, vault_root, logger
                    )
                    vault_summary_path = vault_path_for_source(
                        source_file, codebase_root, vault_root
                    )
                    write_summary(vault_summary_path, summary_str)
                    logger.info("Summarized: %s", source_file)
                except LLMError as exc:
                    # Fatal: rate-limit exhaustion or non-retriable API error
                    console.print(f"\n[red]Fatal LLM error:[/red] {exc}")
                    logger.error("Fatal LLM error on %s: %s", source_file, exc)
                    sys.exit(1)
                except OSError as exc:
                    # Per-file failure: log, record, continue
                    console.print(
                        f"\n[yellow]Warning:[/yellow] Failed to process "
                        f"{source_file.name}: {exc}"
                    )
                    logger.warning("File processing failed %s: %s", source_file, exc)
                    failed_files.append(source_file)
                finally:
                    progress.advance(task)
    else:
        console.print("\n[bold]Phase 2a:[/bold] No files to summarize.")

    # --- Sub-step B: Deletions ---
    if change_set.deleted_summaries:
        console.print(
            f"\n[bold]Phase 2b:[/bold] Removing "
            f"{len(change_set.deleted_summaries)} deleted summary file(s)…"
        )
        deletion_result = apply_deletions(change_set, vault_root, log_fn, logger)
        if deletion_result.deleted_files:
            console.print(
                f"  Deleted: {len(deletion_result.deleted_files)} summary file(s)"
            )
        if deletion_result.backlinks_cleaned:
            total_links = sum(c for _, c in deletion_result.backlinks_cleaned)
            console.print(
                f"  Cleaned: {total_links} dead backlink(s) from "
                f"{len(deletion_result.backlinks_cleaned)} file(s)"
            )
        if deletion_result.failed_deletions:
            console.print(
                f"  [yellow]Warning:[/yellow] "
                f"{len(deletion_result.failed_deletions)} deletion(s) failed"
            )
    else:
        console.print("\n[bold]Phase 2b:[/bold] No deletions.")

    # --- Sub-step C: Rebuild index ---
    console.print("\n[bold]Phase 2c:[/bold] Rebuilding index.md…")
    rebuild_index(vault_root, logger)
    console.print("  index.md updated.")

    # --- Sub-step D: Staleness detection ---
    console.print("\n[bold]Phase 2d:[/bold] Checking query pages for staleness…")

    # Pass the raw ChangeSet and codebase_root; detect_stale_queries() extracts
    # vault paths internally using vault_path_for_source().
    staleness_result = detect_stale_queries(
        change_set, vault_root, codebase_root, log_fn, logger
    )

    # Report malformed Sources pages (hard error per AT-24)
    if staleness_result.malformed_sources_pages:
        console.print(
            f"\n[yellow]Warning:[/yellow] "
            f"{len(staleness_result.malformed_sources_pages)} query page(s) have "
            f"missing or malformed ## Sources section:"
        )
        for page in staleness_result.malformed_sources_pages:
            console.print(f"  - {page.relative_to(vault_root).as_posix()}")
        console.print(
            "  These pages were reported but not flagged as stale. "
            "Review them manually."
        )

    # Report newly-stale pages
    if staleness_result.flagged_pages:
        console.print(
            f"\n[yellow]⚠[/yellow] {len(staleness_result.flagged_pages)} query page(s) "
            f"flagged as stale: "
            + ", ".join(
                str(p.relative_to(vault_root).as_posix())
                for p in staleness_result.flagged_pages
            )
        )
        console.print("  Run [bold]codewiki lint[/bold] to regenerate.")

    return failed_files


# ---------------------------------------------------------------------------
# Helper: completion summary
# ---------------------------------------------------------------------------

def _print_summary(
    change_set: ChangeSet,
    failed_files: list[Path],
    vault_root: Path,
) -> None:
    """Print a human-readable completion summary table and write a log.md entry."""
    from rich.table import Table
    from codebase_wiki_builder.logging_setup import append_log_md
    from datetime import datetime, timezone

    console = Console()
    console.print("\n[bold]Ingest complete.[/bold]")

    table = Table(show_header=True, header_style="bold")
    table.add_column("Category", style="dim")
    table.add_column("Count", justify="right")

    summarized_count = (
        len(change_set.new_files) + len(change_set.modified_files) - len(failed_files)
    )
    table.add_row("Files summarized", str(summarized_count))
    table.add_row("  New", str(len(change_set.new_files)))
    table.add_row("  Modified", str(len(change_set.modified_files)))
    table.add_row("Files skipped (unchanged)", str(len(change_set.skipped_unchanged)))
    table.add_row("Files skipped (binary)", str(len(change_set.skipped_binary)))
    table.add_row("Files skipped (too large)", str(len(change_set.skipped_too_large)))
    table.add_row("Files deleted", str(len(change_set.deleted_summaries)))
    table.add_row("[red]Files failed[/red]", str(len(failed_files)))

    console.print(table)

    if failed_files:
        console.print("\n[red]Failed files:[/red]")
        for f in failed_files:
            console.print(f"  - {f}")

    # Write summary entry to log.md (FR-6.1 format)
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    scanned_count = (
        len(change_set.new_files)
        + len(change_set.modified_files)
        + len(change_set.skipped_unchanged)
        + len(change_set.skipped_binary)
        + len(change_set.skipped_too_large)
    )
    entry = (
        f"{ts} | ingest | "
        f"scanned={scanned_count} "
        f"summarized={summarized_count} "
        f"skipped_unchanged={len(change_set.skipped_unchanged)} "
        f"skipped_too_large={len(change_set.skipped_too_large)} "
        f"skipped_binary={len(change_set.skipped_binary)} "
        f"failed={len(failed_files)} "
        f"deleted={len(change_set.deleted_summaries)}"
    )
    append_log_md(vault_root, entry)


# ---------------------------------------------------------------------------
# analysis subcommand
# ---------------------------------------------------------------------------

@app.command()
def analysis(
    vault_path: Path = typer.Option(
        Path("."),
        "--vault",
        "-v",
        help="Path to the Obsidian vault root (default: current directory).",
        exists=False,  # validated manually for better error messages
    ),
) -> None:
    """Analyze wiki summaries and write overview.md."""
    from codebase_wiki_builder.analysis import run_analysis
    from codebase_wiki_builder.config import load_config
    from codebase_wiki_builder.llm_client import LLMClient, LLMError
    from codebase_wiki_builder.logging_setup import setup_logging, append_log_md

    vault_root = vault_path.resolve()
    if not vault_root.is_dir():
        typer.echo(f"Error: vault directory does not exist: {vault_root}", err=True)
        raise typer.Exit(code=1)

    logger = setup_logging(vault_root)
    log_fn: Callable[[str], None] = lambda entry: append_log_md(vault_root, entry)

    config = load_config(vault_root)  # exits with code 1 on invalid config

    try:
        llm_client = LLMClient(config)
    except LLMError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    console = Console()
    console.print("[bold]Running analysis…[/bold]")

    try:
        run_analysis(vault_root, llm_client, config, logger, log_fn)
    except LLMError as exc:
        logger.error("Fatal LLM error during analysis: %s", exc)
        typer.echo(f"Fatal LLM error: {exc}", err=True)
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Helper: exit code
# ---------------------------------------------------------------------------

def _exit_with_code(failed_files: list[Path]) -> None:
    """Raise typer.Exit with the appropriate exit code.

    Exit codes:
      0 — all files processed successfully
      2 — partial success: at least one file failed to summarize (OSError)

    Exit code 1 is handled inline via sys.exit(1) at the LLMError branch
    and implicitly by load_config() / _validate() (both call sys.exit(1)).
    """
    if failed_files:
        raise typer.Exit(code=2)
    raise typer.Exit(code=0)


# ---------------------------------------------------------------------------
# query subcommand
# ---------------------------------------------------------------------------

@app.command()
def query(
    question: Annotated[str, typer.Argument(help="The question to answer from the wiki.")],
    vault_path: Path = typer.Option(
        Path("."),
        "--vault",
        "-v",
        help="Path to the Obsidian vault root (default: current directory).",
        exists=False,  # validated manually for better error messages
    ),
) -> None:
    """Ask a question answered from the wiki summaries."""
    vault_root = vault_path.resolve()

    if not vault_root.is_dir():
        typer.echo(f"Error: vault directory does not exist: {vault_root}", err=True)
        raise typer.Exit(code=1)

    _run_query_command(question, vault_root)


# ---------------------------------------------------------------------------
# Helper: query orchestration
# ---------------------------------------------------------------------------

def _run_query_command(question: str, vault_root: Path) -> None:
    """Orchestrate the query workflow: setup → run_query → print → prompt → log."""
    from codebase_wiki_builder.config import load_config
    from codebase_wiki_builder.llm_client import LLMClient, LLMError
    from codebase_wiki_builder.logging_setup import setup_logging, append_log_md
    from codebase_wiki_builder.query_engine import run_query, NoRelevantFilesError
    from codebase_wiki_builder.query_persistence import save_query_page
    from datetime import datetime, timezone

    console = Console()

    # 1. Setup logging (creates logs/<timestamp>.log)
    logger = setup_logging(vault_root)
    log_fn: Callable[[str], None] = lambda entry: append_log_md(vault_root, entry)

    # 2. Load config (exits with code 1 if missing or invalid)
    config = load_config(vault_root)

    # 3. Build LLM client — wrap in try/except because constructor validates API key
    try:
        llm_client = LLMClient(config)
    except LLMError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    # 4. Run the query workflow
    #    run_query() raises FileNotFoundError if index.md is missing
    #    run_query() raises NoRelevantFilesError if no relevant files found
    #    run_query() raises LLMError on fatal LLM failure
    try:
        result = run_query(question, vault_root, llm_client, config)
    except FileNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)
    except NoRelevantFilesError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=3)
    except LLMError as exc:
        typer.echo(f"Fatal LLM error: {exc}", err=True)
        raise typer.Exit(code=1)

    # 5. Print stale-page warnings BEFORE the answer (per FR-5)
    if result.stale_warnings:
        stale_list = ", ".join(result.stale_warnings)
        count = len(result.stale_warnings)
        console.print(
            f"[yellow]⚠ {count} query page(s) are stale: {stale_list} — "
            "run codewiki lint to update.[/yellow]"
        )

    # 6. Print the answer
    console.print(result.answer)

    # 7. Prompt user to save (default No)
    save = _prompt_save()

    # 8. Optionally save the query page
    if save:
        saved_path = save_query_page(question, result, vault_root, log_fn)
        rel = saved_path.relative_to(vault_root).as_posix()
        console.print(f"[green]Answer saved to {rel}[/green]")

    # 9. Append per-query log.md entry (FR-6.1)
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    sources_summary = ", ".join(result.sources[:5])
    if len(result.sources) > 5:
        sources_summary += f" (and {len(result.sources) - 5} more)"
    log_fn(f"{ts} | query | {question} | sources: {sources_summary}")

    # 10. Exit 0 — normal completion
    raise typer.Exit(code=0)


# ---------------------------------------------------------------------------
# Helper: save prompt
# ---------------------------------------------------------------------------

def _prompt_save() -> bool:
    """Prompt the user to save the answer. Default is No.

    Returns True if the user answered 'y' or 'Y', False otherwise.
    Handles EOF (non-interactive context) by returning False.
    """
    try:
        response = typer.prompt(
            "Save this answer to the wiki?",
            default="N",
            show_default=True,
        )
        return response.strip() in ("y", "Y")
    except (EOFError, KeyboardInterrupt):
        return False


# ---------------------------------------------------------------------------
# lint subcommand
# ---------------------------------------------------------------------------

@app.command()
def lint(
    vault_path: Path = typer.Option(
        Path("."),
        "--vault",
        "-v",
        help="Path to the Obsidian vault root (default: current directory).",
    ),
) -> None:
    """Resolve stale query pages, deduplicate, and run a deep health-check."""
    from codebase_wiki_builder.config import load_config
    from codebase_wiki_builder.llm_client import LLMClient
    from codebase_wiki_builder.logging_setup import setup_logging, append_log_md

    vault_root = vault_path.resolve()

    # 1. Validate vault directory exists
    if not vault_root.is_dir():
        typer.echo(f"Error: vault directory does not exist: {vault_root}", err=True)
        raise typer.Exit(code=1)

    # 2. Setup logging
    logger = setup_logging(vault_root)
    log_fn: Callable[[str], None] = lambda entry: append_log_md(vault_root, entry)

    # 3. Load config (exits with code 1 on invalid config)
    config = load_config(vault_root)

    # 4. Empty-vault guard: index.md must exist
    index_path = vault_root / "index.md"
    if not index_path.exists():
        typer.echo(
            "Error: The vault has no index. Run 'codewiki ingest' first.",
            err=True,
        )
        raise typer.Exit(code=1)

    # 5. Build LLM client (constructed once; shared across all three parts)
    llm_client = LLMClient(config)

    # 6. Part 1 — Staleness Resolution
    staleness_result = _run_lint_part1(vault_root, llm_client, config, log_fn, logger)

    # 7. Part 2 — Semantic Deduplication
    dedup_result = _run_lint_part2(vault_root, llm_client, log_fn, logger)

    # 8. Part 3 — Deep Health-Check (receives dedup_result for lint-report.md)
    _run_lint_part3(vault_root, llm_client, log_fn, dedup_result, logger)

    # 9. Exit success
    raise typer.Exit(code=0)


def _run_lint_part1(
    vault_root: Path,
    llm_client: "LLMClient",
    config: "WikiConfig",
    log_fn: Callable[[str], None],
    logger: logging.Logger,
) -> "LintStalenessResult":
    """Run lint Part 1: staleness resolution."""
    from codebase_wiki_builder.lint_staleness import resolve_stale_pages
    from rich.console import Console

    console = Console()
    console.print("\n[bold]Lint Part 1:[/bold] Staleness resolution…")
    result = resolve_stale_pages(vault_root, llm_client, config, log_fn)
    logger.info(
        "Lint Part 1 complete: resolved=%d unknowable=%d skipped=%d",
        len(result.resolved_pages),
        len(result.unknowable_pages),
        len(result.skipped_pages),
    )
    return result


def _run_lint_part2(
    vault_root: Path,
    llm_client: "LLMClient",
    log_fn: Callable[[str], None],
    logger: logging.Logger,
) -> "LintDedupResult":
    """Run lint Part 2: semantic deduplication."""
    from codebase_wiki_builder.lint_dedup import deduplicate_query_pages
    from rich.console import Console

    console = Console()
    console.print("\n[bold]Lint Part 2:[/bold] Semantic deduplication…")
    result = deduplicate_query_pages(vault_root, llm_client, log_fn)
    total_merged = sum(len(deleted) for _, deleted in result.merged_groups)
    logger.info(
        "Lint Part 2 complete: merged_groups=%d total_merged=%d skipped=%d",
        len(result.merged_groups),
        total_merged,
        len(result.skipped_pages),
    )
    return result


def _run_lint_part3(
    vault_root: Path,
    llm_client: "LLMClient",
    log_fn: Callable[[str], None],
    dedup_result: "LintDedupResult",
    logger: logging.Logger,
) -> None:
    """Run lint Part 3: deep health-check."""
    from codebase_wiki_builder.lint_healthcheck import run_health_check
    from rich.console import Console

    console = Console()
    console.print("\n[bold]Lint Part 3:[/bold] Deep health-check…")
    run_health_check(vault_root, llm_client, log_fn, dedup_result=dedup_result)
    logger.info("Lint Part 3 complete: lint-report.md written")


# ---------------------------------------------------------------------------
# help subcommand
# ---------------------------------------------------------------------------

_HELP_OVERVIEW = """\
Codebase Wiki Builder — commands:
  ingest    Scan target codebase and update wiki summaries
  analysis  Analyze summaries and write overview.md
  query     Ask a question answered from the wiki
  lint      Resolve stale query pages and health-check the wiki
  help      Show help for commands and MCP setup
"""

_HELP_TOPICS: dict[str, str | None] = {
    "ingest": """\
codewiki ingest [--vault PATH]

Purpose:
  Scan the target codebase and update wiki summaries. Uses a mandatory two-phase
  approach: Phase 1 computes the full change-set without writing anything; Phase 2
  applies all changes (summarize new/modified files, delete removed summaries,
  rebuild index.md, detect stale query pages).

What it reads:
  .wiki-config.json (vault root) — codebase path, LLM provider/model, thresholds
  .env (vault root) — LLM API keys
  All source files in the configured codebase path

What it writes:
  <vault>/<mirrored-path>/<file>.<ext>.md — summary files (new or updated)
  index.md — rebuilt on every run
  log.md — one ingest entry appended
  logs/<timestamp>.log — per-run debug log
  queries/<slug>.md — stale banners inserted (if sources changed)

Exit codes:
  0 — all files processed successfully
  1 — fatal error: invalid config, LLM API failure, vault dir missing
  2 — partial success: one or more files failed to summarize

Notable behaviors:
  - On first run with no .wiki-config.json, prompts interactively for codebase path.
  - Binary files, oversized files, and files in excluded dirs (.git, .venv, etc.)
    are skipped and logged.
  - Query pages whose sources changed are flagged stale (⚠ stale banner after H1);
    run codewiki lint to regenerate them.
""",

    "analysis": """\
codewiki analysis [--vault PATH]

Purpose:
  Read all current summary files, batch them by directory using tiktoken
  (64,000-token limit per batch), send each batch to the LLM for a partial
  overview, then synthesize all partial overviews into a unified root overview.md.

What it reads:
  index.md — checked for stale rows at startup (warning printed if any)
  All summary files in the vault (batched by directory)
  .wiki-config.json, .env

What it writes:
  overview.md (vault root) — overwritten on each run
  <subdir>/overview.md — one per directory batch (overwritten each run)
  index.md — updated with overview entries
  log.md — one analysis entry appended
  logs/<timestamp>.log — per-run debug log

Exit codes:
  0 — analysis completed
  1 — fatal error: index.md missing (run codewiki ingest first), LLM API failure

Notable behaviors:
  - Prints a warning if any query pages are stale, then proceeds normally.
  - Batches summaries by directory tree; subdivides directories that exceed
    the 64,000-token limit.
""",

    "query": """\
codewiki query QUESTION [--vault PATH]

Purpose:
  Answer a natural language question using the wiki. Reads index.md to identify
  relevant summaries via LLM, then sends the question and summaries to the LLM
  for a grounded answer. Offers to save the answer to queries/<slug>.md.

What it reads:
  index.md — checked for stale rows at startup; used to identify relevant files
  Relevant summary files (up to 128,000-token budget, highest relevance first)
  .wiki-config.json, .env

What it writes:
  queries/<slug>.md — only if user answers y at the save prompt (optional)
  index.md — one row appended if answer is saved
  log.md — one query entry appended; one query-saved entry if saved
  logs/<timestamp>.log — per-run debug log

Exit codes:
  0 — answer printed (whether or not the user saved it)
  1 — fatal error: index.md missing (run codewiki ingest first), LLM API failure
  3 — no relevant summaries found for the question

Notable behaviors:
  - Prints a warning if any query pages are stale before answering.
  - Answer always ends with a ## Sources section citing consulted summaries.
  - Summaries exceeding 128,000 tokens individually are listed in ## Sources as
    "(too large to include)".
  - The save prompt defaults to No; press Enter to discard.
  - If the slug already exists, a numeric suffix is appended (e.g., -2.md).
""",

    "lint": """\
codewiki lint [--vault PATH]

Purpose:
  Three-part vault maintenance command:
    Part 1 — Staleness Resolution: re-run the query workflow for each stale page;
             mark unanswerable pages as unknowable.
    Part 2 — Semantic Deduplication: detect and merge near-duplicate query pages.
    Part 3 — Deep Health-Check: batch-analyze vault content for orphans, missing
             cross-references, contradictions, and concept gaps.

What it reads:
  index.md — source of stale rows and query page list
  queries/*.md — stale pages and deduplication candidates (full content in Part 2)
  All summary files (for health-check in Part 3)
  .wiki-config.json, .env

What it writes:
  queries/<slug>.md — overwritten with fresh answers (resolved pages)
  index.md — stale annotations removed; dedup rows replaced; unknowable annotations set
  lint-report.md — overwritten on each run
  log.md — lint-query, lint-resolved, lint-unknowable, lint-deduplicated entries
  logs/<timestamp>.log — per-run debug log

Exit codes:
  0 — lint completed (including pages marked unknowable or merged)
  1 — fatal error: index.md missing, invalid config, LLM API failure, write error

Notable behaviors:
  - Part 1 always runs first, then Part 2, then Part 3; all three run every time.
  - Unknowable pages (zero relevant files after re-run) are flagged with
    ⦸ unknowable in index.md and an [!error] Unknowable banner; lint does not abort.
  - Deduplication uses a conservative threshold; only near-identical pages are merged.
  - lint-report.md includes Orphan Pages, Missing Cross-References, Contradictions,
    Concept Gaps, and Deduplicated Query Pages sections.
""",

    "mcp": None,  # handled separately by _print_help_mcp()
}


@app.command(name="help")
def help_command(
    topic: Optional[str] = typer.Argument(
        None,
        help="Command or topic to get help for (ingest, analysis, query, lint, mcp).",
    ),
) -> None:
    """Show help for commands and MCP setup."""
    if topic is None:
        _print_help_overview()
    elif topic in {"ingest", "analysis", "query", "lint", "mcp"}:
        _print_help_topic(topic)
    else:
        # Unrecognized argument: print error, then general help, exit 0
        typer.echo(f'Error: unrecognized help topic "{topic}"', err=False)
        typer.echo("")
        _print_help_overview()
    raise typer.Exit(code=0)


def _print_help_overview() -> None:
    """Print the command overview table."""
    typer.echo(_HELP_OVERVIEW, nl=False)


def _print_help_topic(topic: str) -> None:
    """Print detailed help for a specific topic."""
    if topic == "mcp":
        _print_help_mcp()
        return
    text = _HELP_TOPICS.get(topic)
    if text:
        typer.echo(text, nl=False)


def _print_help_mcp() -> None:
    """Print MCP setup instructions with runtime-resolved vault path."""
    vault_abs = os.getcwd()

    typer.echo(
        "wiki-mcp — MCP Server for Codebase Wiki Builder\n"
        "\n"
        "The wiki-mcp server exposes a single MCP tool (wiki_query) that lets AI\n"
        "coding agents query your wiki without re-scanning source files. It runs\n"
        "against the vault directory specified by --vault and answers questions\n"
        "using the same logic as `codewiki query`, but returns structured JSON and\n"
        "always saves the answer automatically.\n"
        "\n"
        "To connect Claude Code to this wiki, add the following to\n"
        ".mcp.json in the root of your target codebase:\n"
    )
    typer.echo(
        "{\n"
        '  "mcpServers": {\n'
        '    "wiki": {\n'
        '      "command": "uv",\n'
        f'      "args": ["run", "--project", "/path/to/codebase_wikibuilder", "wiki-mcp", "--vault", "{vault_abs}"]\n'
        "    }\n"
        "  }\n"
        "}\n"
    )
    typer.echo(
        "Then restart Claude Code in the target codebase. Claude Code will\n"
        "automatically discover the wiki_query tool.\n"
        "\n"
        "wiki_query tool schema:\n"
        '  Input:  {"question": str}\n'
        '  Output: {"answer": str, "sources": [str], "saved_path": str,\n'
        '           "stale_warning": [str] | null}\n'
        "\n"
        "Notes:\n"
        "  - wiki-mcp always saves the answer automatically (no save prompt).\n"
        "  - Maintenance commands (ingest, analysis, lint) are CLI-only and are\n"
        "    not exposed via MCP.\n"
        "  - Run `codewiki lint` periodically to deduplicate accumulated query pages.\n"
    )
