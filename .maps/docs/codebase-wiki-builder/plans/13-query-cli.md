# Implementation Plan: Query Command — CLI Wiring

## Spec Context

This plan adds the `query` subcommand to the existing Typer app in `codebase_wiki_builder/cli.py` (created by item 9). It is a pure orchestration layer: it calls `run_query()` from `query_engine.py` (item 11), displays stale-page warnings before printing the answer, prompts the user interactively to save the answer (default No), calls `save_query_page()` (item 12) on `y`/`Y`, appends the per-query `log.md` entry, and exits with the correct exit code (3 if `NoRelevantFilesError` is raised by `run_query`, 0 on normal completion). The CLI catches `FileNotFoundError`, `NoRelevantFilesError`, and `LLMError` from `run_query()` explicitly; `run_query()` does not raise `typer.Exit`.

The `query` command is entirely I/O and orchestration — it contains no query logic. All computation is delegated to `run_query()` and `save_query_page()`.

Catalog item: 13 — Query Command — CLI Wiring
Specification section: FR-5 (CLI-specific: stale warning print, interactive save prompt, default-No behavior, exit code 3 on zero relevant files, exit code 0 on normal completion), FR-6.1 (`query` log entry format)
Acceptance criteria addressed: AT-7 (query command prints answer with `## Sources`), AT-13 (query answer persistence via CLI save prompt), FR-5 (exit codes 0 and 3)

## Dependencies

- **Blocked by**:
  - Item 9 (Ingest CLI) — the Typer `app` object is defined in `cli.py` and must exist before the `query` subcommand is registered
  - Item 11 (Query Core Logic) — needs `run_query()`, `QueryResult`
  - Item 12 (Query Page Persistence) — needs `save_query_page()`
- **Blocks**: None (standalone feature once wired)
- **Uses**: `typer` (CLI framework), `rich` (console output), `pathlib` (stdlib), `sys` (stdlib), `datetime` (stdlib), `logging` (stdlib)

## File Changes

### New Files

- None

### Modified Files

- `codebase_wiki_builder/cli.py` — add `query` subcommand to the existing Typer app; add `_run_query_command()` private orchestration helper and `_prompt_save()` helper

---

## Implementation Details

### Adding the `query` Subcommand

The `query` subcommand is registered on the same `app` instance defined at the top of `cli.py`. Item 9's plan documented this extension pattern:

```python
# At the bottom of cli.py, after the existing ingest command:

@app.command()
def query(
    question: Annotated[str, typer.Argument(help="The question to answer from the wiki.")],
    vault_path: Path = typer.Option(
        Path("."),
        "--vault",
        "-v",
        help="Path to the Obsidian vault root (default: current directory).",
    ),
) -> None:
    """Ask a question answered from the wiki summaries."""
```

`question` is a positional `typer.Argument` — the user passes it as `codewiki query "How does auth work?"`. `vault_path` is the same optional `--vault` flag used by `ingest` for testability.

---

### Subcommand Body

The body delegates to a private helper `_run_query_command()` to keep the decorated function thin (same pattern as `ingest` in item 9):

```python
@app.command()
def query(
    question: Annotated[str, typer.Argument(help="The question to answer from the wiki.")],
    vault_path: Path = typer.Option(
        Path("."),
        "--vault",
        "-v",
        help="Path to the Obsidian vault root (default: current directory).",
    ),
) -> None:
    """Ask a question answered from the wiki summaries."""
    vault_root = vault_path.resolve()

    if not vault_root.is_dir():
        typer.echo(f"Error: vault directory does not exist: {vault_root}", err=True)
        raise typer.Exit(code=1)

    _run_query_command(question, vault_root)
```

---

### `_run_query_command()` — Orchestration Helper

```python
def _run_query_command(question: str, vault_root: Path) -> None:
```

Full orchestration flow:

```python
def _run_query_command(question: str, vault_root: Path) -> None:
    from codebase_wiki_builder.config import load_config
    from codebase_wiki_builder.llm_client import LLMClient
    from codebase_wiki_builder.logging_setup import setup_logging, append_log_md
    from codebase_wiki_builder.query_engine import run_query
    from codebase_wiki_builder.query_persistence import save_query_page
    from datetime import datetime, timezone
    from rich.console import Console

    console = Console()

    # 1. Setup logging (creates logs/<timestamp>.log)
    logger = setup_logging(vault_root)
    log_fn = lambda entry: append_log_md(vault_root, entry)

    # 2. Load config (exits with code 1 if missing or invalid)
    config = load_config(vault_root)

    # 3. Build LLM client (wrap in try/except consistent with ingest and analysis commands)
    from codebase_wiki_builder.llm_client import LLMError
    try:
        llm_client = LLMClient(config)
    except LLMError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    # 4. Run the query workflow
    #    run_query() raises FileNotFoundError if index.md is missing
    #    run_query() raises NoRelevantFilesError if no relevant files found
    #    run_query() raises LLMError on fatal LLM failure
    from codebase_wiki_builder.query_engine import NoRelevantFilesError
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

    # 5. Print stale-page warnings (BEFORE printing the answer, per FR-5)
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
    log_fn(
        f"{ts} | query | {question} | sources: {sources_summary}"
    )

    # 10. Exit 0 — normal completion
    raise typer.Exit(code=0)
```

---

### `_prompt_save()` — Interactive Save Prompt

```python
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
```

`typer.prompt()` with `default="N"` shows `[N]` in the prompt and returns `"N"` when the user presses Enter without input — satisfying the spec's "default No" requirement. `EOFError` is caught to handle non-interactive piped input (e.g., in tests using `CliRunner` without terminal input), defaulting to not saving.

---

### Exit Code Handling

| Exit Code | Trigger | Mechanism |
|-----------|---------|-----------|
| `0` | Normal completion — question answered, user chose not to save or saved successfully | `raise typer.Exit(code=0)` at the end of `_run_query_command()` |
| `1` | `index.md` missing (empty vault), invalid/missing config, `LLMClient` construction failure, or fatal `LLMError` | `FileNotFoundError` or `LLMError` from `run_query()`/`LLMClient` caught → `typer.Exit(code=1)`; `load_config()` calls `sys.exit(1)` |
| `3` | No relevant files found for the question | `NoRelevantFilesError` from `run_query()` caught → `typer.Exit(code=3)` |

The CLI explicitly catches `FileNotFoundError`, `NoRelevantFilesError`, and `LLMError` from `run_query()` and converts them to appropriate exit codes. `run_query()` raises these exceptions rather than using `typer.Exit` directly, keeping `query_engine.py` free of CLI framework dependencies.

---

### Stale Warnings Print Ordering

Per FR-5: "At the START of the `query` command, before doing any other work, the application SHALL read `index.md` and scan for rows containing ` ⚠ stale`… then proceed with the query normally."

The stale warnings are **collected** inside `run_query()` at the start (before any LLM calls) and returned in `QueryResult.stale_warnings`. The CLI **prints** them after `run_query()` returns, but before printing the answer. This satisfies the spec requirement that the warning appears before the answer is shown to the user.

The print ordering in `_run_query_command()` is:
1. `run_query()` returns (stale check happened internally at its start)
2. Print stale warnings if any
3. Print the answer

---

### `log.md` Entry (FR-6.1)

The per-query `log.md` entry is written after the save decision (whether or not the user saves). The `query-saved` entry (if the user saves) is written by `save_query_page()` (item 12) separately. This matches FR-6.1's specification of two distinct entry types: `query` (every run) and `query-saved` (only when saved).

Format for the `query` entry:
```
YYYY-MM-DD HH:MM:SS UTC | query | <question> | sources: <comma-separated source paths>
```

The sources list is truncated to the first 5 with a count note if longer — to keep the log entry readable.

---

### Complete Addition to `cli.py`

```python
# ── Additional imports at top of cli.py (if not already present) ────────────
from typing import Annotated


# ── query subcommand ─────────────────────────────────────────────────────────

@app.command()
def query(
    question: Annotated[str, typer.Argument(help="The question to answer from the wiki.")],
    vault_path: Path = typer.Option(
        Path("."),
        "--vault",
        "-v",
        help="Path to the Obsidian vault root (default: current directory).",
    ),
) -> None:
    """Ask a question answered from the wiki summaries."""
    vault_root = vault_path.resolve()

    if not vault_root.is_dir():
        typer.echo(f"Error: vault directory does not exist: {vault_root}", err=True)
        raise typer.Exit(code=1)

    _run_query_command(question, vault_root)


def _run_query_command(question: str, vault_root: Path) -> None:
    """Orchestrate the query workflow: run_query → print warnings → print answer → prompt save."""
    ...  # implementation as described above


def _prompt_save() -> bool:
    """Prompt the user: 'Save this answer to the wiki? [N]'. Returns True for y/Y."""
    ...  # implementation as described above
```

---

## Error Handling

| Condition | Location | Behavior |
|-----------|----------|----------|
| `vault_root` does not exist | `query()` entry | `typer.echo(error, err=True)` + `typer.Exit(code=1)` |
| `load_config()` finds missing or invalid config | `_run_query_command()` | `load_config()` calls `sys.exit(1)` internally with informative message |
| `LLMClient(config)` raises `LLMError` | `_run_query_command()` step 3 | Caught → `typer.echo(error, err=True)` + `typer.Exit(code=1)` |
| `index.md` missing (empty vault) | `run_query()` raises `FileNotFoundError` | Caught in `_run_query_command()` → message printed to stderr + `typer.Exit(code=1)` |
| LLM API failure (`LLMError`) | `run_query()` propagates `LLMError` | Caught in `_run_query_command()` → "Fatal LLM error" printed + `typer.Exit(code=1)` |
| No relevant files found | `run_query()` raises `NoRelevantFilesError` | Caught in `_run_query_command()` → message printed + `typer.Exit(code=3)` |
| `save_query_page()` raises `OSError` | `_run_query_command()` step 8 | `OSError` propagates — user sees traceback, exit code non-zero. Edge case (disk full, permissions); for MVP, let it propagate. |
| `EOFError` at save prompt (non-interactive) | `_prompt_save()` | Returns `False` — treat as "No"; execution continues normally |
| `KeyboardInterrupt` at save prompt | `_prompt_save()` | Returns `False` — treat as "No"; execution continues normally |

---

## Unit Test Specifications

**File**: `tests/test_cli_query.py`

All tests use `tmp_path` for the vault directory. All LLM calls and core module calls are mocked via `unittest.mock.patch`. The Typer test runner is `typer.testing.CliRunner`.

```python
from typer.testing import CliRunner
from codebase_wiki_builder.cli import app

runner = CliRunner()
```

---

### `query` — exit codes

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Normal completion, no save | Mock `run_query` returns `QueryResult`; mock `_prompt_save` returns `False` | Exit code 0 | FR-5: normal completion exits 0 |
| Normal completion, user saves | Mock `run_query` returns `QueryResult`; mock `_prompt_save` returns `True`; mock `save_query_page` returns a path | Exit code 0 | Save does not change exit code |
| No relevant files | Mock `run_query` raises `NoRelevantFilesError` | Exit code 3 | FR-5: exit code 3 for no relevant files |
| Empty vault (no `index.md`) | Mock `run_query` raises `FileNotFoundError` | Exit code 1 | FR-5: empty vault error |
| Invalid config | Write malformed `.wiki-config.json` | Exit code 1; error message includes field name | Config validation (from `load_config()`) |
| Vault dir does not exist | Pass `--vault /nonexistent` | Exit code 1; error message mentions path | Vault root guard |

---

### `query` — stale warnings printed before answer

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| No stale pages | `result.stale_warnings == []` | No "⚠" warning in output | Only print if stale |
| One stale page | `result.stale_warnings == ["queries/page.md"]` | Output contains "⚠ 1 query page(s) are stale" and "queries/page.md" | FR-5: stale warning before answer |
| Two stale pages | `result.stale_warnings == ["queries/a.md", "queries/b.md"]` | Output contains "⚠ 2 query page(s) are stale" | Multiple stale pages |
| Warning appears before answer | Single stale page | "⚠" line appears before `result.answer` content in output | FR-5: warning precedes answer |

**Key Scenario: stale warning ordering**

```python
def test_stale_warning_before_answer(tmp_path):
    from typer.testing import CliRunner
    from unittest.mock import patch, MagicMock
    from codebase_wiki_builder.cli import app
    from codebase_wiki_builder.query_engine import QueryResult

    vault = tmp_path / "vault"
    vault.mkdir()
    import json
    (vault / ".wiki-config.json").write_text(
        json.dumps({"codebase_path": str(tmp_path / "codebase")})
    )

    result = QueryResult(
        answer="Auth uses JWT tokens.\n\n## Sources\n- src/auth.py.md",
        sources=["src/auth.py.md"],
        one_line_summary="Explains JWT auth",
        stale_warnings=["queries/old-page.md"],
    )

    runner = CliRunner(mix_stderr=False)
    with patch("codebase_wiki_builder.cli.run_query", return_value=result), \
         patch("codebase_wiki_builder.cli.LLMClient"), \
         patch("codebase_wiki_builder.cli._prompt_save", return_value=False), \
         patch("codebase_wiki_builder.cli.append_log_md"):
        res = runner.invoke(app, ["query", "--vault", str(vault), "How does auth work?"])

    assert res.exit_code == 0
    # Warning appears in output
    assert "⚠" in res.output
    assert "queries/old-page.md" in res.output
    # Answer also appears in output
    assert "Auth uses JWT tokens." in res.output
    # Warning appears before answer text
    warning_pos = res.output.find("⚠")
    answer_pos = res.output.find("Auth uses JWT tokens.")
    assert warning_pos < answer_pos
```

---

### `query` — answer printed

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Answer printed to stdout | Mock `run_query` returns result with non-empty answer | `result.answer` content in output | FR-5: answer printed to terminal |
| `## Sources` section in output | `result.answer` ends with `## Sources` block | "## Sources" visible in output | AT-7: answer ends with `## Sources` |

---

### `query` — save prompt behavior

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| User enters `y` | CliRunner with input `"y\n"` | `save_query_page` called once | Saves on y |
| User enters `Y` | CliRunner with input `"Y\n"` | `save_query_page` called once | Case-insensitive y |
| User presses Enter (default No) | CliRunner with input `"\n"` | `save_query_page` NOT called | Default No behavior |
| User enters `n` | CliRunner with input `"n\n"` | `save_query_page` NOT called | Explicit No |
| User enters `N` | CliRunner with input `"N\n"` | `save_query_page` NOT called | Explicit No (uppercase) |
| Non-interactive (EOF) | CliRunner with no input | `save_query_page` NOT called; exit code 0 | Non-interactive safety |

**Key Scenario: default No behavior**

```python
def test_query_default_no_does_not_save(tmp_path):
    from typer.testing import CliRunner
    from unittest.mock import patch, MagicMock
    from codebase_wiki_builder.cli import app
    from codebase_wiki_builder.query_engine import QueryResult

    vault = tmp_path / "vault"
    vault.mkdir()
    import json
    (vault / ".wiki-config.json").write_text(
        json.dumps({"codebase_path": str(tmp_path / "codebase")})
    )

    result = QueryResult(
        answer="Some answer.\n\n## Sources\n- src/foo.py.md",
        sources=["src/foo.py.md"],
        one_line_summary="Some summary",
        stale_warnings=[],
    )

    runner = CliRunner(mix_stderr=False)
    with patch("codebase_wiki_builder.cli.run_query", return_value=result), \
         patch("codebase_wiki_builder.cli.LLMClient"), \
         patch("codebase_wiki_builder.cli.save_query_page") as mock_save, \
         patch("codebase_wiki_builder.cli.append_log_md"):
        # User presses Enter (empty input = default No)
        res = runner.invoke(app, ["query", "--vault", str(vault), "What is foo?"], input="\n")

    assert res.exit_code == 0
    mock_save.assert_not_called()
```

---

### `query` — save path printed on save

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Saved path printed | `_prompt_save` returns True; `save_query_page` returns a path | Output contains vault-relative path of saved file | User feedback |
| Path is vault-relative | Saved path is `vault/queries/how-does-auth-work.md` | Output shows `queries/how-does-auth-work.md` | Not absolute path |

---

### `query` — log.md entry written

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Log entry written on every run | Any successful run | `append_log_md` called with entry containing "query" | FR-6.1 |
| Entry contains question | Question is "How does auth work?" | Log entry contains "How does auth work?" | FR-6.1 |
| Entry contains timestamp | Any run | Entry starts with `YYYY-MM-DD HH:MM:SS UTC` | FR-6.1 |
| Entry contains sources | `result.sources = ["src/auth.py.md"]` | Log entry contains "src/auth.py.md" | FR-6.1 |
| Log entry written even when not saving | `_prompt_save` returns False | `append_log_md` still called | Every query is logged |

---

### `query` — exit code 3 (no relevant files)

**Key Scenario: exit code 3 on empty relevance**

```python
def test_query_exits_3_on_no_relevant_files(tmp_path):
    from typer.testing import CliRunner
    from unittest.mock import patch
    from codebase_wiki_builder.cli import app
    from codebase_wiki_builder.query_engine import NoRelevantFilesError

    vault = tmp_path / "vault"
    vault.mkdir()
    import json
    (vault / ".wiki-config.json").write_text(
        json.dumps({"codebase_path": str(tmp_path / "codebase")})
    )

    runner = CliRunner(mix_stderr=False)
    with patch("codebase_wiki_builder.cli.run_query",
               side_effect=NoRelevantFilesError("No relevant files found for that query.")), \
         patch("codebase_wiki_builder.cli.LLMClient"), \
         patch("codebase_wiki_builder.cli.setup_logging"), \
         patch("codebase_wiki_builder.cli.load_config"):
        res = runner.invoke(app, ["query", "--vault", str(vault), "What is irrelevant?"])

    assert res.exit_code == 3
```

Note: `CliRunner` catches `SystemExit` (including `typer.Exit`) and reports it as `result.exit_code`. The mock raises `NoRelevantFilesError`, which `_run_query_command()` catches and converts to `typer.Exit(code=3)`.

---

### `_prompt_save()` unit tests

| Case | Input | Expected | Why |
|------|-------|----------|-----|
| `"y"` | `"y"` | `True` | Saves on y |
| `"Y"` | `"Y"` | `True` | Case-insensitive |
| `"n"` | `"n"` | `False` | No |
| `"N"` | `"N"` | `False` | No |
| Empty string (Enter) | `""` (default returned by typer.prompt) | `False` | Default No |
| `EOFError` | `EOFError` raised | `False` | Non-interactive |
| `KeyboardInterrupt` | `KeyboardInterrupt` raised | `False` | Ctrl-C safety |

---

## Notes

- **`run_query()` raises standard Python exceptions — not `typer.Exit`**: `run_query()` raises `FileNotFoundError` (missing index), `NoRelevantFilesError` (no relevant files), and `LLMError` (fatal LLM failure). The CLI catches each explicitly in `_run_query_command()` and converts them to appropriate `typer.Exit` codes. This keeps `query_engine.py` free of `typer` dependencies while giving the CLI full control over exit codes and error messages.

- **Stale warnings are collected inside `run_query()`, printed by the CLI**: Per the Notes in item 11's plan: "The caller (CLI item 13) is responsible for displaying them to the terminal after `run_query()` returns; `run_query()` does not print them — it only returns them in `QueryResult`." This keeps `query_engine.py` free of terminal I/O while ensuring the warnings appear before the answer.

- **`typer.prompt()` for the save prompt**: Using `typer.prompt()` rather than Python's built-in `input()` keeps the prompting consistent with Typer conventions and is easier to test with `CliRunner` (which captures I/O). The `default="N"` parameter ensures pressing Enter without input returns `"N"`, satisfying the spec's "default No" behavior. `_prompt_save()` catches `EOFError` and `KeyboardInterrupt` to prevent crashes in non-interactive contexts (e.g., piped input, test harnesses).

- **`save_query_page()` writes both the page and the `query-saved` log entry**: The CLI does not write the `query-saved` log entry itself — `save_query_page()` (item 12) handles it internally. The CLI only writes the generic `query` log entry (recording the question and sources consulted). These two entries are distinct: the `query-saved` entry is only written if the user saves; the `query` entry is always written.

- **Imports are deferred to `_run_query_command()` body**: Core module imports (`query_engine`, `query_persistence`, `llm_client`, `config`, `logging_setup`) are performed inside `_run_query_command()` rather than at module level. This follows the pattern from item 9's `ingest` command — it improves CLI startup time and avoids circular import risks.

- **`Annotated` import**: The `query` subcommand uses `Annotated[str, typer.Argument(...)]` for the `question` parameter. `Annotated` is imported from `typing` (Python 3.11+) or `typing_extensions` — since the project targets Python 3.10+, use `from typing import Annotated` which is available from Python 3.9+.

- **`CliRunner` and `sys.exit()` vs `typer.Exit()`**: `CliRunner` from `typer.testing` catches both `SystemExit` and `typer.Exit` exceptions, converting them to `result.exit_code`. Tests can assert on `result.exit_code` regardless of whether the underlying code uses `sys.exit()` or `raise typer.Exit()`. For consistency with item 9, use `raise typer.Exit(code=N)` in new code in this plan; `sys.exit(1)` only appears in `load_config()` which is external.

- **`_run_query_command()` is a private helper (prefixed `_`)**: It is not exported or used by any other module. This mirrors the `_load_or_prompt_config()`, `_run_phase1()`, `_run_phase2()` pattern from item 9. Tests that need to test it in isolation can import it directly (`from codebase_wiki_builder.cli import _run_query_command`) or test via `CliRunner` end-to-end.

- **No `rich` progress bar for query**: Unlike `ingest`, the query command has no per-file progress to display. The only output before the answer is the optional stale-pages warning, printed with `console.print()` using a yellow style. The answer itself is printed with a plain `console.print(result.answer)`.

- **`log.md` entry written after the save decision**: The generic `query` log entry is written at the end of `_run_query_command()`, after the save prompt. This is intentional — it records whether the answer was saved by including sources (the `query-saved` entry from item 12 records the save separately). If the user hits Ctrl-C at the save prompt, `_prompt_save()` returns `False` and the log entry is still written.
