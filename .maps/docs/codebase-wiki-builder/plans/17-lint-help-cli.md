# Implementation Plan: Lint Command — CLI Wiring and Help Command

## Spec Context

This plan adds two subcommands to the existing Typer app in `codebase_wiki_builder/cli.py`: `lint` and `help`. The `lint` subcommand orchestrates the three-part lint workflow (Parts 1, 2, 3) defined in items 14 and 16. The `help` subcommand provides usage documentation in three forms: no-argument overview table, per-command detail pages, and a special `help mcp` form that emits a runtime-resolved `.mcp.json` snippet.

This module is a pure orchestration and presentation layer. All lint business logic lives in `lint_staleness.py`, `lint_dedup.py`, and `lint_healthcheck.py`. The help subcommand performs no I/O except `os.getcwd()` for path resolution.

Catalog item: 17 — Lint Command — CLI Wiring and Help Command
Specification section: FR-8 (lint command, empty-vault guard, exit codes), FR-10 (help command: all three forms)
Acceptance criteria addressed: AT-14/15 (lint orchestration path), AT-16/17 (dedup + health-check path), AT-18 (`codewiki help mcp` output), AT-25 (`codewiki help foo` unrecognized argument)

## Dependencies

- **Blocked by**:
  - Item 9 (Ingest CLI) — `cli.py` with the Typer `app` must exist; this plan adds to it
  - Item 14 (Lint Part 1) — needs `resolve_stale_pages()`, `LintStalenessResult` from `lint_staleness.py`
  - Item 16 (Lint Part 2 + Part 3) — needs `deduplicate_query_pages()`, `LintDedupResult` from `lint_dedup.py`; needs `run_health_check()` from `lint_healthcheck.py`
- **Uses**: `typer`, `rich.console.Console`, `pathlib.Path`, `os`, `sys` (all stdlib or installed deps); `append_log_md()`, `setup_logging()` from `logging_setup.py`; `load_config()` from `config.py`; `LLMClient` from `llm_client.py`

## File Changes

### New Files

- None

### Modified Files

- `codebase_wiki_builder/cli.py` — Add `lint` subcommand and `help` subcommand to the existing Typer `app`

---

## Implementation Details

### Module Structure

**File**: `codebase_wiki_builder/cli.py`

The Typer `app` was created by item 9. This plan appends two `@app.command()` decorated functions to that same module. No changes to the existing `ingest` subcommand or the `app` definition are needed.

**Exports added** (in addition to those from item 9):
- `lint` — the lint subcommand function (registered via `@app.command()`)
- `help` — the help subcommand function (registered via `@app.command()`)

---

### `lint` Subcommand

**Signature**:

```python
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
```

**Full orchestration flow**:

```python
def lint(vault_path: Path = ...) -> None:
    vault_root = vault_path.resolve()

    # 1. Validate vault directory exists
    if not vault_root.is_dir():
        typer.echo(f"Error: vault directory does not exist: {vault_root}", err=True)
        raise typer.Exit(code=1)

    # 2. Setup logging
    logger = setup_logging(vault_root)
    log_fn = lambda entry: append_log_md(vault_root, entry)

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

    # 5. Build LLM client
    llm_client = LLMClient(config)

    # 6. Part 1 — Staleness Resolution
    staleness_result = _run_lint_part1(vault_root, llm_client, config, log_fn, logger)

    # 7. Part 2 — Semantic Deduplication
    dedup_result = _run_lint_part2(vault_root, llm_client, log_fn, logger)

    # 8. Part 3 — Deep Health-Check (receives dedup_result for lint-report.md)
    _run_lint_part3(vault_root, llm_client, log_fn, dedup_result, logger)

    # 9. Exit success
    raise typer.Exit(code=0)
```

Exit codes:
- `0` — lint completed (all three parts ran, even if some pages were unknowable or skipped)
- `1` — fatal error: invalid config, missing vault dir, missing `index.md`, fatal LLM API error (propagated from any part), or `OSError` writing `lint-report.md`

Per the spec (FR-8): lint exits 0 on success even if stale pages were found and resolved.

---

### Step 6 — `_run_lint_part1()`

```python
def _run_lint_part1(
    vault_root: Path,
    llm_client: "LLMClient",
    config: "WikiConfig",
    log_fn: Callable[[str], None],
    logger: logging.Logger,
) -> "LintStalenessResult":
```

```python
def _run_lint_part1(
    vault_root: Path,
    llm_client: "LLMClient",
    config: "WikiConfig",
    log_fn: Callable[[str], None],
    logger: logging.Logger,
) -> "LintStalenessResult":
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
```

`resolve_stale_pages()` handles all per-page terminal output (✓ Regenerated / ⊘ Unknowable) and the final "Staleness resolved: N pages updated." line internally. The CLI just prints the section header and delegates.

---

### Step 7 — `_run_lint_part2()`

```python
def _run_lint_part2(
    vault_root: Path,
    llm_client: "LLMClient",
    log_fn: Callable[[str], None],
    logger: logging.Logger,
) -> "LintDedupResult":
```

```python
def _run_lint_part2(
    vault_root: Path,
    llm_client: "LLMClient",
    log_fn: Callable[[str], None],
    logger: logging.Logger,
) -> "LintDedupResult":
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
```

`deduplicate_query_pages()` handles all per-merge terminal output (✓ Merged) and the final deduplication summary line internally.

---

### Step 8 — `_run_lint_part3()`

```python
def _run_lint_part3(
    vault_root: Path,
    llm_client: "LLMClient",
    log_fn: Callable[[str], None],
    dedup_result: "LintDedupResult",
    logger: logging.Logger,
) -> None:
```

```python
def _run_lint_part3(
    vault_root: Path,
    llm_client: "LLMClient",
    log_fn: Callable[[str], None],
    dedup_result: "LintDedupResult",
    logger: logging.Logger,
) -> None:
    from codebase_wiki_builder.lint_healthcheck import run_health_check
    from rich.console import Console

    console = Console()
    console.print("\n[bold]Lint Part 3:[/bold] Deep health-check…")
    run_health_check(vault_root, llm_client, log_fn, dedup_result=dedup_result)
    logger.info("Lint Part 3 complete: lint-report.md written")
```

`run_health_check()` writes `lint-report.md` and prints "Lint report written to lint-report.md" internally. It accepts the `dedup_result` so the `## Deduplicated Query Pages` section of `lint-report.md` reflects what happened in Part 2.

---

### `help` Subcommand

**Signature**:

```python
@app.command(name="help")
def help_command(
    topic: Optional[str] = typer.Argument(
        None,
        help="Command or topic to get help for (ingest, analysis, query, lint, mcp).",
    ),
) -> None:
    """Show help for commands and MCP setup."""
```

`name="help"` is required because `help` is a Python built-in; using it as a function name would shadow the built-in. The `@app.command(name="help")` tells Typer the subcommand name is `help` while the Python function is named `help_command`.

**Dispatch logic**:

```python
def help_command(topic: Optional[str] = None) -> None:
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
```

All three forms exit 0 (per FR-10, AT-18, AT-25).

---

### `_print_help_overview()` — Form 1

Prints the command overview table exactly as specified in FR-10.1.

```python
_HELP_OVERVIEW = """\
Codebase Wiki Builder — commands:
  ingest    Scan target codebase and update wiki summaries
  analysis  Analyze summaries and write overview.md
  query     Ask a question answered from the wiki
  lint      Resolve stale query pages and health-check the wiki
  help      Show help for commands and MCP setup
"""


def _print_help_overview() -> None:
    typer.echo(_HELP_OVERVIEW, nl=False)
```

---

### `_print_help_topic()` — Form 2 (per-command detail pages)

Dispatches to topic-specific help text. Each topic includes: purpose, what it reads, what it writes, exit codes, notable behaviors.

```python
_HELP_TOPICS: dict[str, str] = {
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
    ⊘ unknowable in index.md and an [!error] Unknowable banner; lint does not abort.
  - Deduplication uses a conservative threshold; only near-identical pages are merged.
  - lint-report.md includes Orphan Pages, Missing Cross-References, Contradictions,
    Concept Gaps, and Deduplicated Query Pages sections.
""",

    "mcp": None,  # handled separately by _print_help_mcp()
}


def _print_help_topic(topic: str) -> None:
    if topic == "mcp":
        _print_help_mcp()
        return
    text = _HELP_TOPICS.get(topic)
    if text:
        typer.echo(text, nl=False)
```

---

### `_print_help_mcp()` — Form 3 (MCP setup instructions)

Resolves the vault path at runtime using `os.getcwd()` and prints the `.mcp.json` snippet with the path substituted in.

```python
import os


def _print_help_mcp() -> None:
    """Print MCP setup instructions with runtime-resolved vault path."""
    vault_abs = os.getcwd()

    typer.echo(
        "wiki-mcp — MCP Server for Codebase Wiki Builder\n"
        "\n"
        "The wiki-mcp server exposes a single MCP tool (wiki_query) that lets AI\n"
        "coding agents query your wiki without re-scanning source files. It runs\n"
        "against the vault directory you specify via --project and answers questions\n"
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
        f'      "args": ["run", "--project", "{vault_abs}", "wiki-mcp"]\n'
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
```

The vault absolute path comes from `os.getcwd()` at the moment `codewiki help mcp` is invoked, which is the vault root (per the spec requirement that the CLI is always invoked from the vault root). This satisfies AT-18(c): the output contains the resolved absolute path.

---

### Complete Module Additions Skeleton

```python
# Additions to codebase_wiki_builder/cli.py
# (appended after the existing ingest subcommand and its helpers)

from __future__ import annotations

import os
from typing import Optional

# ... existing imports from item 9 ...


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
    ...  # orchestrates _run_lint_part1, _run_lint_part2, _run_lint_part3


def _run_lint_part1(
    vault_root: Path,
    llm_client: "LLMClient",
    config: "WikiConfig",
    log_fn: "Callable[[str], None]",
    logger: "logging.Logger",
) -> "LintStalenessResult": ...


def _run_lint_part2(
    vault_root: Path,
    llm_client: "LLMClient",
    log_fn: "Callable[[str], None]",
    logger: "logging.Logger",
) -> "LintDedupResult": ...


def _run_lint_part3(
    vault_root: Path,
    llm_client: "LLMClient",
    log_fn: "Callable[[str], None]",
    dedup_result: "LintDedupResult",
    logger: "logging.Logger",
) -> None: ...


# ---------------------------------------------------------------------------
# help subcommand
# ---------------------------------------------------------------------------

_HELP_OVERVIEW: str = ...   # module-level constant (see above)
_HELP_TOPICS: dict[str, str | None] = ...  # module-level constant (see above)


@app.command(name="help")
def help_command(
    topic: Optional[str] = typer.Argument(
        None,
        help="Command or topic to get help for (ingest, analysis, query, lint, mcp).",
    ),
) -> None:
    """Show help for commands and MCP setup."""
    ...  # dispatches to _print_help_overview, _print_help_topic, or error path


def _print_help_overview() -> None: ...
def _print_help_topic(topic: str) -> None: ...
def _print_help_mcp() -> None: ...
```

---

## Error Handling

| Condition | Location | Behavior |
|-----------|----------|----------|
| Vault dir does not exist | `lint()` entry | Exit code 1; error printed to stderr |
| `load_config()` invalid config | `lint()` | `load_config()` calls `sys.exit(1)` internally |
| `index.md` missing | `lint()` | Print error message; raise `typer.Exit(code=1)` |
| `LLMError` fatal during Part 1, 2, or 3 | propagates from core modules | `LLMError` propagates to `lint()`; Python prints traceback and exits 1 |
| `OSError` writing `lint-report.md` | Part 3 | Propagates to `lint()`; Python prints traceback and exits 1 |
| `resolve_stale_pages()` per-page skip | Part 1 | Handled internally in `lint_staleness.py`; `lint` continues |
| `deduplicate_query_pages()` per-page skip | Part 2 | Handled internally in `lint_dedup.py`; `lint` continues |
| `help` with unrecognized topic | `help_command()` | Print `Error: unrecognized help topic "…"`, print overview, exit 0 (NOT exit 1) |
| `help mcp` called from non-vault dir | `_print_help_mcp()` | `os.getcwd()` still returns the current directory; output is correct for whatever dir the user is in |

---

## Unit Test Specifications

**File**: `tests/test_cli_lint_help.py`

All tests use `typer.testing.CliRunner`. LLM calls mocked via `unittest.mock.patch`. Vault dirs use `tmp_path`.

---

### Setup helper

```python
from typer.testing import CliRunner
from codebase_wiki_builder.cli import app

runner = CliRunner()
```

---

### `lint` — vault guard and empty-vault guard

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Vault dir missing | `--vault /nonexistent` | Exit code 1; error in output | Guard before logging setup |
| Vault dir exists, no `index.md` | Valid dir, no index.md | Exit code 1; error mentions "codewiki ingest" | FR-8 empty-vault guard |
| `index.md` present | Valid vault with `index.md` | Proceeds past guard | Happy path |

---

### `lint` — orchestration order

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Parts called in order | Track call order via mocks | `resolve_stale_pages` before `deduplicate_query_pages` before `run_health_check` | FR-8 ordering |
| `dedup_result` passed to `run_health_check` | Inspect call args | `run_health_check` receives `dedup_result` kwarg from Part 2 | `lint-report.md` dedup section |
| Exit code 0 on success | All mocks return normally | Exit code 0 | FR-8 exit codes |

---

### `lint` — fatal error propagation

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Invalid config | Malformed `.wiki-config.json` | Exit code 1 | `load_config()` exits 1 |
| Fatal LLM error in Part 1 | Mock `resolve_stale_pages` raises `LLMError` | Exit code 1 | Fatal API failure propagates |

---

### `lint` — full integration scenario

```python
def test_lint_full_orchestration(tmp_path):
    from unittest.mock import patch, MagicMock, call
    from codebase_wiki_builder.cli import app
    from codebase_wiki_builder.lint_staleness import LintStalenessResult
    from codebase_wiki_builder.lint_dedup import LintDedupResult

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "index.md").write_text("| File | Description |\n|------|-------------|\n")

    import json
    (vault / ".wiki-config.json").write_text(
        json.dumps({"codebase_path": str(tmp_path / "codebase")})
    )
    (tmp_path / "codebase").mkdir()

    staleness_result = LintStalenessResult(
        resolved_pages=[], unknowable_pages=[], skipped_pages=[]
    )
    dedup_result = LintDedupResult(merged_groups=[], skipped_pages=[])

    call_order = []

    def fake_part1(*args, **kwargs):
        call_order.append("part1")
        return staleness_result

    def fake_part2(*args, **kwargs):
        call_order.append("part2")
        return dedup_result

    def fake_part3(*args, dedup_result=None, **kwargs):
        call_order.append("part3")
        (vault / "lint-report.md").write_text("# Wiki Lint Report\n")

    runner = CliRunner()
    with patch("codebase_wiki_builder.cli.resolve_stale_pages", side_effect=fake_part1), \
         patch("codebase_wiki_builder.cli.deduplicate_query_pages", side_effect=fake_part2), \
         patch("codebase_wiki_builder.cli.run_health_check", side_effect=fake_part3), \
         patch("codebase_wiki_builder.cli.LLMClient"):
        result = runner.invoke(app, ["lint", "--vault", str(vault)])

    assert result.exit_code == 0
    assert call_order == ["part1", "part2", "part3"]
```

---

### `help` — overview form (no argument)

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| `codewiki help` (no arg) | Plain invocation | Output contains "ingest", "analysis", "query", "lint", "help"; exit code 0 | FR-10.1 |
| All five commands listed | No arg | Output contains all five subcommand names | FR-10.1 complete list |

```python
def test_help_overview():
    from codebase_wiki_builder.cli import app
    runner = CliRunner()
    result = runner.invoke(app, ["help"])
    assert result.exit_code == 0
    for cmd in ["ingest", "analysis", "query", "lint", "help"]:
        assert cmd in result.output
```

---

### `help` — per-command forms

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| `codewiki help ingest` | `help ingest` | Output contains "Purpose", "What it reads", "What it writes", "Exit codes"; exit 0 | FR-10.2 |
| `codewiki help analysis` | `help analysis` | Same four sections; exit 0 | FR-10.2 |
| `codewiki help query` | `help query` | Same four sections; exit 0 | FR-10.2 |
| `codewiki help lint` | `help lint` | Same four sections; exit 0 | FR-10.2 |

```python
@pytest.mark.parametrize("topic", ["ingest", "analysis", "query", "lint"])
def test_help_per_command(topic):
    from codebase_wiki_builder.cli import app
    runner = CliRunner()
    result = runner.invoke(app, ["help", topic])
    assert result.exit_code == 0
    for section in ["Purpose", "What it reads", "What it writes", "Exit codes"]:
        assert section in result.output
```

---

### `help mcp` form (AT-18)

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Exits code 0 | `help mcp` | Exit code 0 | AT-18(a) |
| Contains JSON block with `mcpServers` | `help mcp` | `"mcpServers"` in output | AT-18(b) |
| Contains resolved vault path | `help mcp` from known cwd | `os.getcwd()` value in output | AT-18(c) |

```python
def test_help_mcp_output(tmp_path, monkeypatch):
    import os
    from codebase_wiki_builder.cli import app
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["help", "mcp"])
    assert result.exit_code == 0
    assert "mcpServers" in result.output
    assert str(tmp_path) in result.output
```

**Note on `os.getcwd()` in tests**: The `CliRunner` does not change the process working directory. Use `monkeypatch.chdir(tmp_path)` to set a known directory, then verify the absolute path appears in the output.

---

### `help` — unrecognized argument (AT-25)

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| `codewiki help foo` | Unrecognized topic | Output contains `Error: unrecognized help topic "foo"`; output contains general help table; exit code 0 | AT-25(a)(b)(c) |
| Error message identifies the argument | `help bar` | Output contains `"bar"` in error message | AT-25(a) |
| General help table shown | `help foo` | All five command names in output | AT-25(b) |
| Exit code 0 (not 1) | Any unrecognized | Exit code 0 | AT-25(c) |

```python
def test_help_unrecognized_argument():
    from codebase_wiki_builder.cli import app
    runner = CliRunner()
    result = runner.invoke(app, ["help", "foo"])
    assert result.exit_code == 0
    assert 'unrecognized help topic "foo"' in result.output
    for cmd in ["ingest", "analysis", "query", "lint", "help"]:
        assert cmd in result.output
```

---

### Key Scenario: Lint with stale pages (AT-14 at the CLI level)

```python
def test_lint_calls_part1_with_correct_args(tmp_path):
    from unittest.mock import patch, MagicMock
    import json
    from codebase_wiki_builder.cli import app
    from codebase_wiki_builder.lint_staleness import LintStalenessResult
    from codebase_wiki_builder.lint_dedup import LintDedupResult

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "index.md").write_text("| File | Description |\n|------|-------------|\n")
    (tmp_path / "codebase").mkdir()
    (vault / ".wiki-config.json").write_text(
        json.dumps({"codebase_path": str(tmp_path / "codebase")})
    )

    mock_part1 = MagicMock(
        return_value=LintStalenessResult(resolved_pages=[], unknowable_pages=[], skipped_pages=[])
    )
    mock_part2 = MagicMock(
        return_value=LintDedupResult(merged_groups=[], skipped_pages=[])
    )
    mock_part3 = MagicMock()

    runner = CliRunner()
    with patch("codebase_wiki_builder.cli.resolve_stale_pages", mock_part1), \
         patch("codebase_wiki_builder.cli.deduplicate_query_pages", mock_part2), \
         patch("codebase_wiki_builder.cli.run_health_check", mock_part3), \
         patch("codebase_wiki_builder.cli.LLMClient"):
        result = runner.invoke(app, ["lint", "--vault", str(vault)])

    assert result.exit_code == 0
    # Part 1 receives vault_root, llm_client, config, log_fn
    assert mock_part1.call_count == 1
    call_kwargs = mock_part1.call_args
    assert call_kwargs.args[0] == vault  # vault_root is first positional

    # Part 3 receives dedup_result from Part 2
    part3_kwargs = mock_part3.call_args
    assert "dedup_result" in part3_kwargs.kwargs or len(part3_kwargs.args) >= 4
```

---

## Notes

- **`help` Python built-in conflict**: Python's built-in `help` would be shadowed if the function were named `help`. Using `help_command` as the function name and `@app.command(name="help")` to set the CLI-visible name resolves this cleanly. Typer dispatches based on the `name` kwarg, not the function name.

- **`os.getcwd()` in `_print_help_mcp()`**: The spec says "resolve the current working directory (vault root) as an absolute path at runtime." `os.getcwd()` returns the process working directory at the time of invocation — correct when `codewiki` is run from the vault root. `Path(".").resolve()` would be equivalent but `os.getcwd()` is more explicit. Do not use `vault_path` from a CLI option here; `help mcp` takes no vault option and must use the actual cwd.

- **`_HELP_TOPICS["mcp"] = None`**: The `mcp` topic is special-cased in `_print_help_topic()` to call `_print_help_mcp()` rather than printing a static string. This is because the mcp output includes a runtime-resolved path. All other topics use static strings.

- **Section header labels in help text**: The per-command help pages use `Purpose:`, `What it reads:`, `What it writes:`, `Exit codes:`, and `Notable behaviors:` as section headers. These match FR-10.2's required elements by name. Tests check for these exact strings.

- **Deferred imports inside helper functions**: Per the established pattern in `cli.py` (item 9), imports of core modules (`lint_staleness`, `lint_dedup`, `lint_healthcheck`) are deferred to the `_run_lint_part*()` helper bodies rather than at module top-level. This avoids circular import issues during test collection and speeds up CLI startup.

- **`LLMClient` instantiation is shared across all three parts**: The `LLMClient` instance is constructed once in `lint()` and passed to all three part-running helpers. This ensures the inter-request delay (enforced by `LLMClient` internally) is correctly applied across the entire lint run, not reset between parts.

- **`log_fn` construction pattern**: Same as `ingest`: `log_fn = lambda entry: append_log_md(vault_root, entry)`. Passed to all three part helpers so they can append to `log.md`.

- **Exit code 0 even with unknowable pages**: Per FR-8, lint exits 0 on success "even if stale pages were found and resolved." This includes the unknowable case. Only fatal errors (missing `index.md`, invalid config, LLM API failure, write error) cause exit code 1.

- **`help` always exits 0**: All three forms of `codewiki help` exit with code 0, including the unrecognized-argument form (AT-25(c)). The `raise typer.Exit(code=0)` at the end of `help_command()` is unconditional.

- **`CliRunner` and `monkeypatch.chdir` for `help mcp` tests**: `CliRunner` does not change the process working directory, so `os.getcwd()` inside `_print_help_mcp()` returns the real cwd during tests. Using `monkeypatch.chdir(tmp_path)` sets a predictable value that can be asserted in the output. Alternatively, `_print_help_mcp()` can be tested by patching `os.getcwd` directly.

- **Section header label "Notable behaviors" in help pages**: FR-10.2 lists the required elements but does not mandate exact label wording. The labels chosen (`Purpose`, `What it reads`, `What it writes`, `Exit codes`, `Notable behaviors`) are clear and descriptive; the tests check for these. If the Critic or Test Writer require different labels, adjust accordingly.
