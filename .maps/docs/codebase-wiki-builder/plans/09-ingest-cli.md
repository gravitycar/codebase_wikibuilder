# Implementation Plan: Ingest Command — CLI Wiring

## Spec Context

This plan implements `codebase_wiki_builder/cli.py`: the Typer application definition, the `ingest` subcommand, and the `codewiki` entry point. It orchestrates the full two-phase ingest workflow by calling into the core modules (scanner, summarizer, deletion, index writer, staleness detector), managing first-run config prompting, displaying progress via `rich`, printing a completion summary, and returning correct exit codes (0 = success, 1 = fatal error, 2 = partial success with per-file failures).

This module is a pure orchestration layer — it contains no business logic of its own. All domain operations are delegated to already-planned modules. It is the integration seam that connects Phase 1 (item 5) through Phase 2 (items 6–8) into a single coherent command.

Catalog item: 9 — Ingest Command — CLI Wiring
Specification section: FR-1 (CLI entry point, progress, completion summary), FR-2 (first-run config prompting), FR-3 preamble (mandatory two-phase execution), FR-3.6, FR-3.7, FR-3.8, Non-functional exit codes
Acceptance criteria addressed: AT-1 (fresh ingest), AT-2 (incremental ingest), AT-8 (first-run prompt), FR-1 (progress display), FR-3 (two-phase mandatory ordering), exit codes 0/1/2

## Dependencies

- **Blocked by**:
  - Item 2 (Configuration Model) — needs `load_config()`, `prompt_for_config()`, `WikiConfig`, `CONFIG_FILENAME`
  - Item 4 (Vault File Utilities + Logging) — needs `setup_logging()`, `append_log_md()`, `vault_path_for_source()`
  - Item 5 (Scanner) — needs `scan_codebase()`, `ChangeSet`
  - Item 6 (Summarizer) — needs `summarize_file()`, `write_summary()`
  - Item 7 (Deletion) — needs `apply_deletions()`
  - Item 8 (Index + Staleness) — needs `rebuild_index()`, `detect_stale_queries()`, `StalenessResult`
- **Blocks**: Item 13 (Query CLI), Item 17 (Lint CLI + Help) — those plans add subcommands to the Typer app defined here
- **Uses**: `typer` (CLI framework), `rich` (progress display, bundled with Typer), `pathlib` (stdlib), `sys` (stdlib), `datetime` (stdlib)

## File Changes

### New Files

- `codebase_wiki_builder/cli.py` — Typer app definition, `ingest` subcommand, `codewiki` entry point

### Modified Files

- None (this is the first CLI module; the Typer app is created here and extended by items 13 and 17)

---

## Implementation Details

### Module Structure

**File**: `codebase_wiki_builder/cli.py`

**Exports**:
- `app` — the Typer application instance (imported by `pyproject.toml` entry point and by items 13 and 17 when adding subcommands)

**Entry point** (in `pyproject.toml`):
```
codewiki = "codebase_wiki_builder.cli:app"
```

The module defines the Typer `app`, then immediately registers the `ingest` subcommand. Later items (13 and 17) will import `app` from this module and call `app.command()` to register additional subcommands. This is the standard Typer pattern for multi-file Typer apps.

---

### Typer App Definition

```python
import typer

app = typer.Typer(
    name="codewiki",
    help="Codebase Wiki Builder — manage your Obsidian wiki.",
    add_completion=False,
    no_args_is_help=True,
)
```

`add_completion=False` disables shell completion generation (not needed for this tool). `no_args_is_help=True` prints the help text when `codewiki` is invoked with no subcommand.

---

### `ingest` Subcommand

**Signature**:

```python
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
```

The vault root defaults to the current working directory (`.`), matching the spec requirement that the CLI is always invoked from the vault root. The `--vault` flag is optional and exists for testability and edge cases; normal usage does not pass it.

**Full orchestration flow** (described in detail below):

```python
def ingest(vault_path: Path = ...) -> None:
    vault_root = vault_path.resolve()

    # 1. Setup logging (creates logs/<timestamp>.log)
    logger = setup_logging(vault_root)
    log_fn = lambda entry: append_log_md(vault_root, entry)

    # 2. Load or create config (first-run prompt if missing)
    config = _load_or_prompt_config(vault_root, logger)

    # 3. Phase 1 — compute change-set (no vault writes)
    change_set = _run_phase1(config, vault_root, logger)

    # 4. Phase 2 — apply changes
    failed_files = _run_phase2(change_set, config, vault_root, log_fn, logger)

    # 5. Print completion summary and exit
    _print_summary(change_set, failed_files, vault_root)
    _exit_with_code(failed_files)
```

---

### Step 1: Logging Setup

```python
from codebase_wiki_builder.logging_setup import setup_logging, append_log_md

logger = setup_logging(vault_root)
log_fn = lambda entry: append_log_md(vault_root, entry)
```

`setup_logging()` creates `logs/<YYYY-MM-DD_HH-MM-SS>.log` and returns the application-wide logger. `log_fn` is a closure used by Phase 2 modules to append entries to `log.md`.

---

### Step 2: Config Loading — `_load_or_prompt_config()`

```python
def _load_or_prompt_config(vault_root: Path, logger: logging.Logger) -> WikiConfig:
```

The `ingest` command is the only command that handles the missing-config case by prompting. Other commands call `load_config()` directly (which exits with code 1 if the file is missing).

```python
def _load_or_prompt_config(vault_root: Path, logger: logging.Logger) -> WikiConfig:
    from codebase_wiki_builder.config import (
        CONFIG_FILENAME,
        WikiConfig,
        load_config,
        prompt_for_config,
    )

    config_path = vault_root / CONFIG_FILENAME
    if not config_path.exists():
        logger.info("No config file found at %s; prompting for first-run setup", config_path)
        config = prompt_for_config(vault_root)
    else:
        config = load_config(vault_root)  # exits with code 1 if invalid

    logger.info(
        "Config loaded: codebase=%s provider=%s model=%s",
        config.codebase_path, config.llm_provider, config.llm_model,
    )
    return config
```

`load_config()` calls `sys.exit(1)` internally if the config is invalid — so the CLI does not need to handle config validation errors; they are already handled.

---

### Step 3: Phase 1 — `_run_phase1()`

```python
def _run_phase1(
    config: WikiConfig,
    vault_root: Path,
    logger: logging.Logger,
) -> ChangeSet:
```

Calls `scan_codebase()` and displays a brief progress message. Phase 1 makes no vault changes — it only computes the change-set.

```python
def _run_phase1(
    config: WikiConfig,
    vault_root: Path,
    logger: logging.Logger,
) -> ChangeSet:
    from codebase_wiki_builder.scanner import scan_codebase
    from rich.console import Console

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
```

---

### Step 4: Phase 2 — `_run_phase2()`

```python
def _run_phase2(
    change_set: ChangeSet,
    config: WikiConfig,
    vault_root: Path,
    log_fn: Callable[[str], None],
    logger: logging.Logger,
) -> list[Path]:
```

Returns a list of source file paths that failed to be summarized (each OSError or LLMError per file is caught here). A non-empty list means exit code 2. A fatal `LLMError` (rate-limit exhaustion or non-retriable API error) causes `sys.exit(1)` immediately.

Phase 2 has four sequential sub-steps:

**Sub-step A: Summarize new/modified files**

```python
def _run_phase2(
    change_set: ChangeSet,
    config: WikiConfig,
    vault_root: Path,
    log_fn: Callable[[str], None],
    logger: logging.Logger,
) -> list[Path]:
    from codebase_wiki_builder.llm_client import LLMClient, LLMError
    from codebase_wiki_builder.summarizer import summarize_file, write_summary
    from codebase_wiki_builder.deletion import apply_deletions
    from codebase_wiki_builder.index_writer import rebuild_index
    from codebase_wiki_builder.staleness import detect_stale_queries
    from codebase_wiki_builder.vault import vault_path_for_source
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

    console = Console()
    codebase_root = Path(config.codebase_path)
    failed_files: list[Path] = []

    # Build LLM client
    llm_client = LLMClient(config)

    # --- Sub-step A: Summarization ---
    files_to_summarize = change_set.new_files + change_set.modified_files
    if files_to_summarize:
        console.print(f"\n[bold]Phase 2a:[/bold] Summarizing {len(files_to_summarize)} file(s)…")
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
                    summary_str = summarize_file(source_file, llm_client, config, vault_root, logger)
                    vault_summary_path = vault_path_for_source(source_file, codebase_root, vault_root)
                    write_summary(vault_summary_path, summary_str)
                    logger.info("Summarized: %s", source_file)
                except LLMError as exc:
                    # Fatal: rate-limit exhaustion or non-retriable API error
                    console.print(f"\n[red]Fatal LLM error:[/red] {exc}")
                    logger.error("Fatal LLM error on %s: %s", source_file, exc)
                    sys.exit(1)
                except OSError as exc:
                    # Per-file failure: log, record, continue
                    console.print(f"\n[yellow]Warning:[/yellow] Failed to process {source_file.name}: {exc}")
                    logger.warning("File processing failed %s: %s", source_file, exc)
                    failed_files.append(source_file)
                finally:
                    progress.advance(task)
    else:
        console.print("\n[bold]Phase 2a:[/bold] No files to summarize.")
```

The `LLMError` branch triggers `sys.exit(1)` immediately — this is the fatal exit path. `OSError` per file is non-fatal: the file is counted as failed and ingest continues, contributing to exit code 2.

**Sub-step B: Apply deletions**

```python
    # --- Sub-step B: Deletions ---
    if change_set.deleted_summaries:
        console.print(f"\n[bold]Phase 2b:[/bold] Removing {len(change_set.deleted_summaries)} deleted summary file(s)…")
        deletion_result = apply_deletions(change_set, vault_root, log_fn, logger)
        if deletion_result.deleted_files:
            console.print(f"  Deleted: {len(deletion_result.deleted_files)} summary file(s)")
        if deletion_result.backlinks_cleaned:
            total_links = sum(c for _, c in deletion_result.backlinks_cleaned)
            console.print(f"  Cleaned: {total_links} dead backlink(s) from {len(deletion_result.backlinks_cleaned)} file(s)")
        if deletion_result.failed_deletions:
            console.print(f"  [yellow]Warning:[/yellow] {len(deletion_result.failed_deletions)} deletion(s) failed")
    else:
        console.print("\n[bold]Phase 2b:[/bold] No deletions.")
```

**Sub-step C: Rebuild index.md**

```python
    # --- Sub-step C: Rebuild index ---
    console.print("\n[bold]Phase 2c:[/bold] Rebuilding index.md…")
    rebuild_index(vault_root, logger)
    console.print("  index.md updated.")
```

**Sub-step D: Staleness detection**

The staleness detector requires a `changed_vault_paths: set[str]` (vault-relative path strings). The CLI constructs this set from the `ChangeSet` before calling `detect_stale_queries()`.

```python
    # --- Sub-step D: Staleness detection ---
    console.print("\n[bold]Phase 2d:[/bold] Checking query pages for staleness…")

    # Pass the raw ChangeSet and codebase_root; detect_stale_queries() extracts vault paths internally
    staleness_result = detect_stale_queries(change_set, vault_root, codebase_root, log_fn, logger)

    # Report malformed Sources pages (hard error per AT-24)
    if staleness_result.malformed_sources_pages:
        console.print(
            f"\n[yellow]Warning:[/yellow] {len(staleness_result.malformed_sources_pages)} query page(s) have missing or malformed ## Sources section:"
        )
        for page in staleness_result.malformed_sources_pages:
            console.print(f"  - {page.relative_to(vault_root).as_posix()}")
        console.print("  These pages were reported but not flagged as stale. Review them manually.")

    # Report newly-stale pages
    if staleness_result.flagged_pages:
        console.print(
            f"\n[yellow]⚠[/yellow] {len(staleness_result.flagged_pages)} query page(s) flagged as stale: "
            + ", ".join(str(p.relative_to(vault_root).as_posix()) for p in staleness_result.flagged_pages)
        )
        console.print("  Run [bold]codewiki lint[/bold] to regenerate.")

    return failed_files
```

**Return**: The list of source files that failed summarization (empty = no failures).

---

### Step 5: Completion Summary — `_print_summary()`

```python
def _print_summary(
    change_set: ChangeSet,
    failed_files: list[Path],
    vault_root: Path,
) -> None:
```

Prints a human-readable summary table to the terminal after all Phase 2 steps complete.

```python
def _print_summary(
    change_set: ChangeSet,
    failed_files: list[Path],
    vault_root: Path,
) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    console.print("\n[bold]Ingest complete.[/bold]")

    table = Table(show_header=True, header_style="bold")
    table.add_column("Category", style="dim")
    table.add_column("Count", justify="right")

    table.add_row("Files summarized",    str(len(change_set.new_files) + len(change_set.modified_files) - len(failed_files)))
    table.add_row("  New",               str(len(change_set.new_files)))
    table.add_row("  Modified",          str(len(change_set.modified_files)))
    table.add_row("Files skipped (unchanged)",  str(len(change_set.skipped_unchanged)))
    table.add_row("Files skipped (binary)",     str(len(change_set.skipped_binary)))
    table.add_row("Files skipped (too large)",  str(len(change_set.skipped_too_large)))
    table.add_row("Files deleted",       str(len(change_set.deleted_summaries)))
    table.add_row("[red]Files failed[/red]",    str(len(failed_files)))

    console.print(table)

    if failed_files:
        console.print("\n[red]Failed files:[/red]")
        for f in failed_files:
            console.print(f"  - {f}")

    # Write summary entry to log.md
    from codebase_wiki_builder.logging_setup import append_log_md
    from datetime import datetime, timezone

    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = (
        f"{ts} | ingest | "
        f"scanned={len(change_set.new_files) + len(change_set.modified_files) + len(change_set.skipped_unchanged) + len(change_set.skipped_binary) + len(change_set.skipped_too_large)} "
        f"summarized={len(change_set.new_files) + len(change_set.modified_files) - len(failed_files)} "
        f"skipped_unchanged={len(change_set.skipped_unchanged)} "
        f"skipped_too_large={len(change_set.skipped_too_large)} "
        f"skipped_binary={len(change_set.skipped_binary)} "
        f"failed={len(failed_files)} "
        f"deleted={len(change_set.deleted_summaries)}"
    )
    append_log_md(vault_root, entry)
```

The `log.md` entry format matches FR-6.1: all ingest counts included in one line.

---

### Step 6: Exit Code Logic — `_exit_with_code()`

```python
def _exit_with_code(failed_files: list[Path]) -> None:
    if failed_files:
        raise typer.Exit(code=2)
    raise typer.Exit(code=0)
```

Using `typer.Exit` rather than `sys.exit()` is idiomatic in Typer and allows the test suite to catch the exception without ending the process. Exit code 1 is handled inline via `sys.exit(1)` at the `LLMError` branch (fatal error) and implicitly by `load_config()` / `_validate()` (also calls `sys.exit(1)`).

Exit code summary:
- `0` — all files processed successfully (no failures)
- `1` — fatal error: LLM API failure (rate-limit exhaustion or non-retriable), or invalid config (from `load_config()`)
- `2` — partial success: at least one file failed to summarize due to `OSError`

---

### Complete Module Skeleton

```python
from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console

if TYPE_CHECKING:
    from codebase_wiki_builder.config import WikiConfig
    from codebase_wiki_builder.scanner import ChangeSet

app = typer.Typer(
    name="codewiki",
    help="Codebase Wiki Builder — manage your Obsidian wiki.",
    add_completion=False,
    no_args_is_help=True,
)


@app.command()
def ingest(
    vault_path: Path = typer.Option(
        Path("."),
        "--vault", "-v",
        help="Path to the Obsidian vault root (default: current directory).",
    ),
) -> None:
    """Scan the target codebase and update wiki summaries."""
    ...  # orchestrates _load_or_prompt_config, _run_phase1, _run_phase2, _print_summary, _exit_with_code


def _load_or_prompt_config(vault_root: Path, logger: logging.Logger) -> WikiConfig: ...
def _run_phase1(config: WikiConfig, vault_root: Path, logger: logging.Logger) -> ChangeSet: ...
def _run_phase2(
    change_set: ChangeSet,
    config: WikiConfig,
    vault_root: Path,
    log_fn: Callable[[str], None],
    logger: logging.Logger,
) -> list[Path]: ...
def _print_summary(change_set: ChangeSet, failed_files: list[Path], vault_root: Path) -> None: ...
def _exit_with_code(failed_files: list[Path]) -> None: ...
```

Items 13 (query CLI) and 17 (lint + help CLI) will import `app` from this module and register additional subcommands:

```python
# In cli.py additions from items 13 and 17:
from codebase_wiki_builder.cli import app

@app.command()
def query(...): ...

@app.command()
def lint(...): ...

@app.command()
def help(...): ...
```

---

## Error Handling

| Condition | Location | Behavior |
|-----------|----------|----------|
| `load_config()` finds invalid config | `_load_or_prompt_config()` | `load_config()` calls `sys.exit(1)` internally with informative message |
| `prompt_for_config()` receives EOF/Ctrl-C | `_load_or_prompt_config()` | `EOFError`/`KeyboardInterrupt` propagates — Python exits naturally |
| `scan_codebase()` returns immediately on non-existent codebase | `_run_phase1()` | `os.walk()` on non-existent path yields nothing — Phase 1 returns empty `ChangeSet`. This should not occur because `load_config()` validates `codebase_path` is a readable directory. |
| `LLMError` during summarization | `_run_phase2()` sub-step A | Fatal: print error, call `sys.exit(1)` |
| `OSError` on individual file read or write | `_run_phase2()` sub-step A | Non-fatal: file added to `failed_files`; ingest continues |
| `apply_deletions()` individual deletion failure | `_run_phase2()` sub-step B | Handled inside `apply_deletions()` (logged at ERROR); deletion result tracked in `DeletionResult.failed_deletions`; CLI reports count in summary |
| `rebuild_index()` raises `OSError` | `_run_phase2()` sub-step C | Propagates to `ingest()`; Python prints traceback. This is an unrecoverable vault state issue. Future plans may add explicit handling here. |
| `detect_stale_queries()` encounters malformed `## Sources` | `_run_phase2()` sub-step D | `StalenessResult.malformed_sources_pages` populated; CLI reports affected pages in terminal summary; ingest does not exit with code 1 solely due to this (per AT-24d) |
| Vault root does not exist | `ingest()` entry | `setup_logging()` will attempt to create `logs/` under it; `OSError` propagates. CLI should add a guard: `if not vault_root.is_dir(): ...` |

**Vault root guard** (add to `ingest()` before `setup_logging()`):

```python
vault_root = vault_path.resolve()
if not vault_root.is_dir():
    typer.echo(f"Error: vault directory does not exist: {vault_root}", err=True)
    raise typer.Exit(code=1)
```

---

## Unit Test Specifications

**File**: `tests/test_cli_ingest.py`

All tests use `tmp_path` for both vault and codebase directories. LLM calls are mocked via `unittest.mock.patch`. The Typer test runner is `typer.testing.CliRunner`.

---

### Setup helper

```python
from typer.testing import CliRunner
from codebase_wiki_builder.cli import app

runner = CliRunner()
```

---

### `ingest` — first-run config prompt

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| No config file; valid path entered | Fresh vault; mock `input()` to return a valid codebase dir | Exit code 0; `.wiki-config.json` created; no error output | AT-8: first-run prompt creates config |
| No config file; mock LLM returns valid response | Same + mock `LLMClient.complete` | Summary files created; exit code 0 | Full first-run happy path |

---

### `ingest` — exit codes

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| All files succeed | Mock LLM returns valid JSON; all source files exist | Exit code 0 | No failures |
| One file fails (OSError) | Mock `summarize_file` raises `OSError` on one file | Exit code 2 | Partial success |
| All files fail (OSError on all) | Mock `summarize_file` raises `OSError` on every file | Exit code 2 (not 1, since OSError is non-fatal) | All per-file failures = partial |
| Fatal LLM error | Mock `LLMClient.complete` raises `LLMError` | Exit code 1 | Fatal API failure |
| Invalid config | Write malformed `.wiki-config.json` | Exit code 1; error message mentions field name | Config validation |

---

### `ingest` — two-phase ordering

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Phase 1 runs before Phase 2 | Track call order via mocks | `scan_codebase` called before `summarize_file`, `apply_deletions`, `rebuild_index`, `detect_stale_queries` | FR-3 preamble: mandatory two-phase |
| Deletions run after summarization | Track call order | `apply_deletions` called after `write_summary` | Phase 2 ordering |
| Index rebuilt after deletions | Track call order | `rebuild_index` called after `apply_deletions` | FR-3.6: post-deletion index |
| Staleness run after index rebuild | Track call order | `detect_stale_queries` called after `rebuild_index` | FR-3.8: staleness uses rebuilt index |

---

### `ingest` — progress and summary output

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Phase labels printed | Standard run | Output contains "Phase 1:", "Phase 2a:", "Phase 2b:", "Phase 2c:", "Phase 2d:" | User visibility |
| Summary table printed | Any run | Output contains "Files summarized", "Files skipped", "Files deleted" | FR-1: completion summary |
| log.md entry written | Standard run | `log.md` exists with `ingest` entry matching FR-6.1 format | FR-6.1 |
| Debug log created | Standard run | `logs/` dir contains one `.log` file | FR-6.2 |

---

### `ingest` — staleness terminal output

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Stale pages reported | Mock `detect_stale_queries` returns flagged pages | Output contains "⚠" and page names and "codewiki lint" hint | FR-3.8 step 5 |
| Malformed Sources reported | Mock returns malformed_sources_pages | Output warns about malformed Sources; page name included | AT-24(c) |
| No stale pages: no stale output | Clean run with no flagged pages | No "⚠" stale warning in output | Only print if stale |

---

### `ingest` — staleness detection call

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Raw ChangeSet passed to staleness | Any ingest run | `detect_stale_queries` called with `change_set`, `vault_root`, `codebase_root` (not a pre-computed `set[str]`) | `staleness.py` handles path conversion internally |
| New files trigger staleness | `change_set.new_files` has one source | Staleness module computes vault path and checks it | FR-3.8: new sources trigger staleness |
| Modified files trigger staleness | `change_set.modified_files` has one source | Vault path derived internally in `detect_stale_queries` | FR-3.8: modified sources trigger staleness |
| Deleted summaries trigger staleness | `change_set.deleted_summaries` has one vault path | Included in internal changed_vault_paths set | FR-3.8: deleted summaries trigger staleness |

---

### `ingest` — vault root guard

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Vault dir does not exist | Pass `--vault /nonexistent/path` | Exit code 1; error message mentions the path | Guard before logging setup |
| Vault dir exists | Pass valid `--vault` path | Proceeds normally | Happy path |

---

### Key Scenario: Full ingest with one failure

**Setup**:
- Fake vault dir and codebase dir (both `tmp_path` subdirs)
- Two source files: `codebase/a.py` (exists), `codebase/b.py` (exists)
- Mock `summarize_file`: succeeds for `a.py`, raises `OSError("disk full")` for `b.py`
- Mock `write_summary`: no-op
- Mock `apply_deletions`, `rebuild_index`, `detect_stale_queries`: no-op
- `.wiki-config.json` present with valid content pointing to the codebase dir

**Action**: `runner.invoke(app, ["ingest", "--vault", str(vault_dir)])`

**Expected**:
- Exit code 2 (partial success)
- Output contains "Failed" and `b.py`
- `log.md` has entry with `failed=1`

```python
def test_ingest_partial_failure(tmp_path):
    from typer.testing import CliRunner
    from unittest.mock import patch, MagicMock
    from codebase_wiki_builder.cli import app
    from codebase_wiki_builder.scanner import ChangeSet

    vault = tmp_path / "vault"
    codebase = tmp_path / "codebase"
    vault.mkdir()
    codebase.mkdir()

    # Write minimal valid config
    import json
    (vault / ".wiki-config.json").write_text(
        json.dumps({"codebase_path": str(codebase)})
    )

    # Two source files
    a = codebase / "a.py"
    b = codebase / "b.py"
    a.write_text("print('a')")
    b.write_text("print('b')")

    fake_change_set = ChangeSet(new_files=[a, b])

    def fake_summarize(path, *args, **kwargs):
        if path == b:
            raise OSError("disk full")
        return "# a.py\n\nSome summary.\n\n## References\n\n<!-- md5: abc -->"

    runner = CliRunner()
    with patch("codebase_wiki_builder.cli.scan_codebase", return_value=fake_change_set), \
         patch("codebase_wiki_builder.cli.summarize_file", side_effect=fake_summarize), \
         patch("codebase_wiki_builder.cli.write_summary"), \
         patch("codebase_wiki_builder.cli.apply_deletions", return_value=MagicMock(deleted_files=[], backlinks_cleaned=[], failed_deletions=[], removed_dirs=[])), \
         patch("codebase_wiki_builder.cli.rebuild_index"), \
         patch("codebase_wiki_builder.cli.detect_stale_queries", return_value=MagicMock(flagged_pages=[], malformed_sources_pages=[], already_stale_pages=[], clean_pages=[])), \
         patch("codebase_wiki_builder.cli.LLMClient"):
        result = runner.invoke(app, ["ingest", "--vault", str(vault)])

    assert result.exit_code == 2
    assert "b.py" in result.output
```

---

### Key Scenario: First-run config prompt

```python
def test_ingest_first_run_prompts_for_config(tmp_path):
    from typer.testing import CliRunner
    from unittest.mock import patch, MagicMock
    from codebase_wiki_builder.cli import app
    from codebase_wiki_builder.scanner import ChangeSet

    vault = tmp_path / "vault"
    codebase = tmp_path / "codebase"
    vault.mkdir()
    codebase.mkdir()

    # No .wiki-config.json — first run

    runner = CliRunner(mix_stderr=False)
    with patch("codebase_wiki_builder.config.input", return_value=str(codebase)), \
         patch("codebase_wiki_builder.cli.scan_codebase", return_value=ChangeSet()), \
         patch("codebase_wiki_builder.cli.apply_deletions", return_value=MagicMock(deleted_files=[], backlinks_cleaned=[], failed_deletions=[], removed_dirs=[])), \
         patch("codebase_wiki_builder.cli.rebuild_index"), \
         patch("codebase_wiki_builder.cli.detect_stale_queries", return_value=MagicMock(flagged_pages=[], malformed_sources_pages=[], already_stale_pages=[], clean_pages=[])), \
         patch("codebase_wiki_builder.cli.LLMClient"):
        result = runner.invoke(app, ["ingest", "--vault", str(vault)])

    assert result.exit_code == 0
    assert (vault / ".wiki-config.json").exists()
```

---

## Notes

- **`app` is defined in this module and extended by items 13 and 17**: Items 13 (query command) and 17 (lint + help commands) import `app` from `cli.py` and call `@app.command()` to register additional subcommands. This is the standard Typer multi-file pattern and requires no changes to this module when those commands are added.

- **`sys.exit(1)` for fatal LLM errors vs. `typer.Exit(code=2)` for partial failures**: Fatal errors (LLM API exhaustion, non-retriable error) use `sys.exit(1)` because they are unrecoverable and should terminate immediately. Per-file failures use `typer.Exit(code=2)` via the `_exit_with_code()` helper, which is testable without ending the test process (Typer's `CliRunner` catches `typer.Exit`). `load_config()` and `_validate()` also use `sys.exit(1)` internally, which `CliRunner` also catches correctly.

- **Imports are deferred to function bodies**: Core module imports (`scanner`, `summarizer`, `deletion`, `index_writer`, `staleness`, `llm_client`) are done inside `_run_phase1()` and `_run_phase2()` rather than at module level. This improves startup time for the CLI (Typer only needs to parse the command signature before dispatching) and avoids circular import risks during the test suite's module loading phase.

- **`detect_stale_queries()` receives the raw `ChangeSet` and `codebase_root`**: The staleness module (item 8) extracts vault-relative path strings internally using `vault_path_for_source()`. The CLI simply passes `change_set`, `vault_root`, and `codebase_root` (derived from `config.codebase_path`). This avoids duplicating the path-conversion logic in the CLI.

- **`rich` progress bar during summarization**: The `Progress` context manager from `rich` is used only during the summarization sub-step (sub-step A), which is the only step with per-file progress to show. Other steps (deletion, index rebuild, staleness) complete quickly and show only a single status line. Using a progress bar for sub-step A gives the user feedback during potentially long LLM API call sequences.

- **`LLMClient` is instantiated once and reused**: The LLM client (from item 3) manages the inter-request delay and retry logic internally. Instantiating it once in `_run_phase2()` and passing it to `summarize_file()` for each file ensures the delay is correctly enforced between consecutive LLM calls.

- **log.md entry format matches FR-6.1 exactly**: The ingest log entry includes all required counters: `scanned`, `summarized`, `skipped_unchanged`, `skipped_too_large`, `skipped_binary`, `failed`, `deleted`. The "scanned" count is the sum of all non-excluded files (new + modified + unchanged + binary + too_large); binary and too_large files are "scanned" in the sense that the scanner processed them.

- **`apply_deletions()` failures are not counted toward exit code 2**: Per-file summarization failures trigger exit code 2. Deletion failures (a file that cannot be unlinked) are reported in the terminal summary but do not affect the exit code — they are tracked in `DeletionResult.failed_deletions` and logged, but the spec exit code table specifies exit code 2 only for "ingest completed but one or more files failed to summarize", not for failed deletions.

- **`_print_summary()` writes the `log.md` entry**: All vault write operations are collected at the end of `ingest()`. The summary function is responsible for the final `log.md` append, which is correct because it has access to all counts needed for the FR-6.1 format.

- **Typer's `CliRunner` for testing**: `CliRunner` from `typer.testing` (which wraps Click's test runner) is used in all `cli.py` tests. It captures stdout/stderr without spawning a subprocess and catches `typer.Exit` exceptions, making it ideal for testing exit codes and output. `sys.exit(1)` calls from `load_config()` or the fatal LLM error branch are also caught by `CliRunner` (it converts `SystemExit` to `result.exit_code`).

- **No Obsidian CLI invocation in this plan**: Item 18 (Obsidian CLI integration) is optional and independent. The `ingest` command does not depend on Obsidian being installed. When item 18 is built, it will add an optional call to `try_enable_search_plugin()` at the start of the `ingest` command (or as a common pre-run hook). This plan leaves a natural insertion point before `_run_phase1()`.
