# Implementation Plan: Analysis Command

## Spec Context

This plan implements the `analysis` subcommand, which reads all summary files from the vault, batches them by directory tree using `tiktoken` (64,000-token limit), sends each batch to the LLM to produce partial overviews, writes per-directory `overview.md` files, synthesizes a unified root `overview.md` from all partial overviews, updates `index.md`, and appends a log entry. It also prints a stale-page warning at startup if any `index.md` rows are flagged ` ⚠ stale`.

Catalog item: 10 — Analysis Command
Specification section: FR-4 (all sub-requirements), FR-6.1 (analysis log entry), Technical Context (ANALYSIS_CONTEXT_WINDOW = 64,000, tiktoken)
Acceptance criteria addressed: AT-6 (analysis produces non-empty `overview.md`, log entry written), FR-4 (stale warning, empty-vault error exit 1, tiktoken batching, subdirectory overview files, root overview.md, index update)

## Dependencies

- **Blocked by**:
  - Item 3 (LLM Client Abstraction) — needs `LLMClient`, `LLMError`
  - Item 4 (Vault File Utilities + Logging) — needs `setup_logging()`, `append_log_md()`, `wikilink()`, `EXCLUDED_DIRS`
  - Item 8 (Index + Staleness) — `index.md` must exist and be accurate before analysis runs; also needs `_parse_existing_index()` pattern for reading stale-flag rows
  - Item 9 (Ingest CLI) — the Typer `app` object must exist in `cli.py` before the `analysis` subcommand can be added to it
- **Blocks**: None (standalone feature once wired)
- **Uses**: `tiktoken` (token counting), `pathlib` (stdlib), `logging` (stdlib), `os` (stdlib), `datetime` (stdlib), `re` (stdlib)

## File Changes

### New Files

- `codebase_wiki_builder/analysis.py` — `run_analysis(vault_root, llm_client, config)`: all analysis business logic (stale warning check, empty-vault guard, tiktoken batching, partial overview generation, subdirectory writes, root synthesis, index update, log entry)

### Modified Files

- `codebase_wiki_builder/cli.py` — add `analysis` subcommand (calls `run_analysis()`, handles `LLMError`, loads config, sets up logging, manages exit codes)

---

## Implementation Details

### Module-Level Constant

**File**: `codebase_wiki_builder/analysis.py`

```python
ANALYSIS_CONTEXT_WINDOW = 64_000  # tokens; hardcoded per spec
```

This constant is also referenced by the lint health-check (item 16). It lives here as the authoritative source; item 16 imports it.

---

### `run_analysis()` — Main Entry Point

**File**: `codebase_wiki_builder/analysis.py`

**Signature**:

```python
def run_analysis(
    vault_root: Path,
    llm_client: LLMClient,
    config: WikiConfig,
    logger: logging.Logger,
    log_fn: Callable[[str], None],
) -> None:
    """Run the full analysis workflow.

    Steps:
      1. Check index.md exists (hard error + sys.exit(1) if absent)
      2. Scan index.md for stale rows; print warning if any found
      3. Collect all summary files from vault
      4. Batch summaries by directory tree using tiktoken
      5. For each batch, send to LLM → partial overview string
      6. Write per-directory overview.md files
      7. Synthesize all partial overviews into root overview.md
      8. Update index.md with all overview.md entries
      9. Append analysis log entry to log.md
    """
```

The function takes an already-constructed `LLMClient` and logger (created by the CLI wiring). It raises `LLMError` on fatal LLM failures (the CLI catches this and calls `sys.exit(1)`).

---

### Step 1: Empty-Vault Guard

```python
index_path = vault_root / "index.md"
if not index_path.exists():
    typer.echo(
        "The vault has no summaries. Run 'codewiki ingest' first.",
        err=True,
    )
    raise typer.Exit(code=1)
```

This matches the exact error message pattern specified in FR-4 and mirrors the guard in the query command.

---

### Step 2: Stale-Row Warning

Read `index.md` and scan for rows containing ` ⚠ stale` in the Description column. Print the warning, then continue — this is informational only and never blocks analysis.

```python
def _check_stale_rows(index_path: Path) -> list[str]:
    """Return vault-relative paths of stale query pages found in index.md."""
    stale_pages: list[str] = []
    try:
        content = index_path.read_text(encoding="utf-8")
    except OSError:
        return stale_pages

    _TABLE_ROW_RE = re.compile(r"^\|\s*(\[\[.*?\]\])\s*\|\s*(.*?)\s*\|$")
    _WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")

    for line in content.splitlines():
        m = _TABLE_ROW_RE.match(line.strip())
        if m and "⚠ stale" in m.group(2):
            inner = _WIKILINK_RE.search(m.group(1))
            if inner:
                stale_pages.append(inner.group(1) + ".md")
    return stale_pages
```

Caller:

```python
stale_pages = _check_stale_rows(index_path)
if stale_pages:
    count = len(stale_pages)
    names = ", ".join(stale_pages)
    typer.echo(
        f"⚠ {count} query page(s) are stale: {names} — run codewiki lint to update."
    )
```

---

### Step 3: Collect Summary Files

Walk the vault, applying the same exclusion rules used by `index_writer.py` (exclude `logs/`, `queries/`, files named `overview.md`, `index.md`, `log.md`, `lint-report.md`). Return a list of `(vault_relative_dir: str, absolute_path: Path)` tuples.

```python
from codebase_wiki_builder.vault import VAULT_SPECIAL_FILES, VAULT_EXCLUDED_DIRS


def collect_summary_files(vault_root: Path) -> list[tuple[str, Path]]:
    """Return (vault_relative_dir_posix, absolute_path) for each summary file.

    vault_relative_dir_posix is the POSIX string of the directory relative
    to vault_root, e.g. "src/auth" for vault_root/src/auth/login.py.md.
    Root-level files have vault_relative_dir_posix = "".
    """
    results: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(vault_root):
        # Prune excluded dirs in-place
        dirnames[:] = [
            d for d in dirnames
            if d not in VAULT_EXCLUDED_DIRS
        ]
        current_dir = Path(dirpath)
        try:
            rel_dir = current_dir.relative_to(vault_root)
        except ValueError:
            continue
        rel_dir_posix = rel_dir.as_posix() if rel_dir != Path(".") else ""

        for filename in filenames:
            if not filename.endswith(".md"):
                continue
            if filename in VAULT_SPECIAL_FILES:
                continue
            if filename == "overview.md":
                continue
            results.append((rel_dir_posix, current_dir / filename))

    return results
```

---

### Step 4: Tiktoken Batching by Directory Tree

This is the core algorithm. The goal: group summary files into batches that each fit within `ANALYSIS_CONTEXT_WINDOW` tokens, respecting directory boundaries.

**Token counting helper**:

```python
def _count_tokens(text: str, encoding_name: str = "cl100k_base") -> int:
    """Count tokens in text using tiktoken."""
    import tiktoken
    enc = tiktoken.get_encoding(encoding_name)
    return len(enc.encode(text))
```

The encoding `cl100k_base` is used by `gpt-4` / `claude` family models and is appropriate for token estimation. The encoding is fetched once per call; for performance, callers should cache the encoder if calling many times (but for MVP, per-call is fine given the I/O dominance).

**Batch data structures**:

```python
@dataclass
class AnalysisBatch:
    """One batch to send to the LLM for a partial overview."""
    vault_dir: str          # vault-relative POSIX dir string (e.g. "src/auth")
    file_paths: list[Path]  # absolute paths of summary files in this batch
    contents: list[str]     # file contents (parallel to file_paths)
    token_count: int        # estimated token count for the combined content
```

**Batching algorithm**:

```python
def build_batches(
    summary_files: list[tuple[str, Path]],
    vault_root: Path,
    logger: logging.Logger,
) -> list[AnalysisBatch]:
    """Group summary files into batches by directory.

    Strategy (per FR-4):
    1. Group files by their top-level vault directory (first path segment).
    2. If a group fits in ANALYSIS_CONTEXT_WINDOW → one batch.
    3. If a group exceeds the window → subdivide into immediate subdirectories
       and repeat until each subdivision fits.
    4. Continue recursively until each batch fits or a single file is
       irreducibly too large (include it alone with a warning).
    """
```

Implementation approach — iterative subdivision:

```python
def build_batches(
    summary_files: list[tuple[str, Path]],
    vault_root: Path,
    logger: logging.Logger,
) -> list[AnalysisBatch]:
    # Group files by top-level directory (first segment of rel_dir)
    # Files at vault root (rel_dir == "") are their own group: ""
    from collections import defaultdict

    def top_level_dir(rel_dir: str) -> str:
        if not rel_dir:
            return ""
        return rel_dir.split("/")[0]

    top_groups: dict[str, list[tuple[str, Path]]] = defaultdict(list)
    for rel_dir, path in summary_files:
        top_groups[top_level_dir(rel_dir)].append((rel_dir, path))

    batches: list[AnalysisBatch] = []
    for top_dir, group in sorted(top_groups.items()):
        _subdivide_into_batches(group, top_dir, vault_root, batches, logger)

    return batches


def _subdivide_into_batches(
    files: list[tuple[str, Path]],
    group_dir: str,
    vault_root: Path,
    batches: list[AnalysisBatch],
    logger: logging.Logger,
) -> None:
    """Recursively subdivide files into batches that fit the context window."""
    if not files:
        return

    # Load file contents and compute total token count
    contents: list[str] = []
    paths: list[Path] = []
    for _, path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Cannot read summary file %s: %s", path, exc)
            text = ""
        contents.append(text)
        paths.append(path)

    combined_text = "\n\n".join(contents)
    total_tokens = _count_tokens(combined_text)

    if total_tokens <= ANALYSIS_CONTEXT_WINDOW:
        # Fits: create one batch for this directory group
        batches.append(AnalysisBatch(
            vault_dir=group_dir,
            file_paths=paths,
            contents=contents,
            token_count=total_tokens,
        ))
        return

    # Too large: subdivide by immediate subdirectory
    # Compute one level deeper than group_dir
    sub_groups: dict[str, list[tuple[str, Path]]] = defaultdict(list)
    for rel_dir, path in files:
        sub_key = _immediate_subdir(rel_dir, group_dir)
        sub_groups[sub_key].append((rel_dir, path))

    if len(sub_groups) == 1:
        # Cannot subdivide further (all files in same dir, or single file)
        # Include as one batch anyway; LLM call may exceed window but this
        # is unavoidable. Log a warning.
        logger.warning(
            "Batch for '%s' exceeds context window (%d tokens) but cannot be "
            "subdivided further. Sending anyway.",
            group_dir, total_tokens,
        )
        batches.append(AnalysisBatch(
            vault_dir=group_dir,
            file_paths=paths,
            contents=contents,
            token_count=total_tokens,
        ))
        return

    for sub_dir, sub_files in sorted(sub_groups.items()):
        _subdivide_into_batches(sub_files, sub_dir, vault_root, batches, logger)


def _immediate_subdir(rel_dir: str, parent_dir: str) -> str:
    """Return the key one level below parent_dir for rel_dir.

    E.g. parent="src", rel_dir="src/auth/utils" → "src/auth"
         parent="", rel_dir="src" → "src"
         parent="", rel_dir="" → "" (file at root stays at root)
    """
    if parent_dir == "":
        # Split on first "/" to get immediate child
        parts = rel_dir.split("/", 1)
        return parts[0]
    # Strip parent_dir prefix and take next segment
    suffix = rel_dir[len(parent_dir):].lstrip("/")
    if not suffix:
        return parent_dir
    next_segment = suffix.split("/")[0]
    return f"{parent_dir}/{next_segment}" if parent_dir else next_segment
```

---

### Step 5 & 6: LLM Call Per Batch → Write Per-Directory `overview.md`

For each `AnalysisBatch`, send its combined summary content to the LLM with the partial-overview prompt, then write the result to `<vault_dir>/overview.md`.

**Partial overview prompt**:

```python
# PARTIAL_OVERVIEW_PROMPT is kept as documentation only — do NOT use with .format() at runtime.
# Use _build_partial_overview_prompt() which constructs the prompt via an f-string.
PARTIAL_OVERVIEW_PROMPT = """\
You are analyzing a subset of summary files from a codebase wiki. \
The files below come from the directory: {vault_dir}

Produce a concise overview (3–8 paragraphs) covering:
- The apparent purpose of code in this directory
- Dominant software engineering patterns observed
- Consistency or inconsistency in the code
- Any notable observations or potential issues

Do not fabricate details not present in the summaries below.

--- SUMMARIES ---
{combined_summaries}
--- END SUMMARIES ---
"""


def _build_partial_overview_prompt(vault_dir_label: str, combined_summaries: str) -> str:
    """Build the partial overview prompt using an f-string.

    Uses an f-string rather than PARTIAL_OVERVIEW_PROMPT.format() so that curly braces
    in untrusted content (LLM-generated summary text) cannot corrupt the prompt or raise
    KeyError at the Python layer.
    """
    return (
        "You are analyzing a subset of summary files from a codebase wiki. "
        f"The files below come from the directory: {vault_dir_label}\n"
        "\n"
        "Produce a concise overview (3–8 paragraphs) covering:\n"
        "- The apparent purpose of code in this directory\n"
        "- Dominant software engineering patterns observed\n"
        "- Consistency or inconsistency in the code\n"
        "- Any notable observations or potential issues\n"
        "\n"
        "Do not fabricate details not present in the summaries below.\n"
        "\n"
        "--- SUMMARIES ---\n"
        f"{combined_summaries}\n"
        "--- END SUMMARIES ---\n"
    )
```

**Per-batch processing**:

```python
def _process_batch(
    batch: AnalysisBatch,
    vault_root: Path,
    llm_client: LLMClient,
    logger: logging.Logger,
) -> str:
    """Send batch to LLM and return partial overview text. Writes overview.md."""
    vault_dir_label = batch.vault_dir if batch.vault_dir else "(root)"
    logger.info(
        "Processing batch for '%s': %d files, ~%d tokens",
        vault_dir_label, len(batch.file_paths), batch.token_count,
    )

    combined = "\n\n---\n\n".join(
        f"File: {path.name}\n\n{content}"
        for path, content in zip(batch.file_paths, batch.contents)
    )
    prompt = _build_partial_overview_prompt(vault_dir_label, combined)

    overview_text = llm_client.complete(prompt)  # raises LLMError on fatal failure

    # Write per-directory overview.md
    if batch.vault_dir:
        overview_dir = vault_root / Path(batch.vault_dir)
    else:
        # Root-level files: overview goes at vault root (handled by synthesis step)
        # But if this batch IS the only batch (all root files), we still write root overview.md
        overview_dir = vault_root
    overview_dir.mkdir(parents=True, exist_ok=True)
    overview_path = overview_dir / "overview.md"
    overview_path.write_text(overview_text, encoding="utf-8")
    logger.info("Wrote overview.md to %s", overview_path)

    return overview_text
```

---

### Step 7: Synthesize Root `overview.md`

After all batches are processed, collect all partial overviews (including any written for root-level file groups) and send them to the LLM for synthesis.

**Synthesis prompt**:

```python
# SYNTHESIS_PROMPT is kept as documentation only — do NOT use with .format() at runtime.
# Use _build_synthesis_prompt() which constructs the prompt via an f-string.
SYNTHESIS_PROMPT = """\
You are synthesizing directory-level overviews into a unified top-level overview \
of an entire codebase. Each section below is an overview of one directory.

Produce a comprehensive overview (5–10 paragraphs) covering:
- The overall apparent purpose of the application
- Dominant software engineering patterns observed across the codebase
- Consistency or inconsistency across modules
- Any notable observations or potential issues

--- DIRECTORY OVERVIEWS ---
{partial_overviews}
--- END DIRECTORY OVERVIEWS ---
"""


def _build_synthesis_prompt(partial_overviews_text: str) -> str:
    """Build the synthesis prompt using an f-string.

    Uses an f-string rather than SYNTHESIS_PROMPT.format() so that curly braces
    in untrusted LLM-generated partial overview text cannot corrupt the prompt or
    raise KeyError at the Python layer.
    """
    return (
        "You are synthesizing directory-level overviews into a unified top-level overview "
        "of an entire codebase. Each section below is an overview of one directory.\n"
        "\n"
        "Produce a comprehensive overview (5–10 paragraphs) covering:\n"
        "- The overall apparent purpose of the application\n"
        "- Dominant software engineering patterns observed across the codebase\n"
        "- Consistency or inconsistency across modules\n"
        "- Any notable observations or potential issues\n"
        "\n"
        "--- DIRECTORY OVERVIEWS ---\n"
        f"{partial_overviews_text}\n"
        "--- END DIRECTORY OVERVIEWS ---\n"
    )
```

**Synthesis call**:

```python
def _synthesize_root_overview(
    partial_overviews: list[tuple[str, str]],   # (vault_dir, overview_text)
    vault_root: Path,
    llm_client: LLMClient,
    logger: logging.Logger,
) -> None:
    """Synthesize all partial overviews into root overview.md."""
    if not partial_overviews:
        logger.warning("No partial overviews to synthesize; root overview.md will be empty.")
        return

    combined_sections = "\n\n---\n\n".join(
        f"Directory: {vault_dir if vault_dir else '(root)'}\n\n{text}"
        for vault_dir, text in partial_overviews
    )
    prompt = _build_synthesis_prompt(combined_sections)

    root_overview = llm_client.complete(prompt)

    root_overview_path = vault_root / "overview.md"
    root_overview_path.write_text(root_overview, encoding="utf-8")
    logger.info("Wrote root overview.md")
```

---

### Step 8: Update `index.md`

After writing all `overview.md` files, update `index.md` to include rows for each new/updated overview. The index update reads the current `index.md`, adds or replaces overview rows, and writes the file back.

Because `overview.md` files may already have rows in `index.md` from prior analysis runs, the update strategy is: re-collect all overview files in the vault and rebuild the overview section. The simplest safe approach: call a helper that adds missing overview rows and updates descriptions for existing ones.

```python
def _update_index_with_overviews(
    vault_root: Path,
    written_overview_paths: list[Path],
    logger: logging.Logger,
) -> None:
    """Add or update overview.md rows in index.md.

    For each written overview, compute its wikilink and description.
    If a row for that wikilink already exists in index.md, update its description.
    If no row exists, append a new row.
    """
    from codebase_wiki_builder.vault import wikilink as make_wikilink

    index_path = vault_root / "index.md"
    if not index_path.exists():
        logger.warning("index.md not found; cannot update with overview entries")
        return

    try:
        content = index_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Cannot read index.md: %s", exc)
        return

    lines = content.splitlines(keepends=True)

    for overview_path in written_overview_paths:
        link = make_wikilink(overview_path, vault_root)
        # Determine description
        if overview_path.parent == vault_root:
            desc = "Top-level application overview"
        else:
            rel_dir = overview_path.parent.relative_to(vault_root)
            desc = f"Directory overview: {rel_dir.as_posix()}/"

        # Check if row exists
        link_target = overview_path.relative_to(vault_root).with_suffix("").as_posix()
        row_exists = any(f"[[{link_target}]]" in line for line in lines)

        new_row = f"| {link} | {desc} |\n"
        if row_exists:
            # Replace existing row
            lines = [
                new_row if f"[[{link_target}]]" in line else line
                for line in lines
            ]
        else:
            # Append new row before end of file
            lines.append(new_row)

    try:
        index_path.write_text("".join(lines), encoding="utf-8")
        logger.info("Updated index.md with %d overview entries", len(written_overview_paths))
    except OSError as exc:
        logger.error("Cannot write updated index.md: %s", exc)
```

---

### Step 9: Append Log Entry

```python
def _write_analysis_log_entry(
    vault_root: Path,
    summary_count: int,
    log_fn: Callable[[str], None],
) -> None:
    from datetime import datetime, timezone
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = f"{ts} | analysis | summaries_reviewed={summary_count}"
    log_fn(entry)
```

---

### Complete `run_analysis()` Body

```python
def run_analysis(
    vault_root: Path,
    llm_client: LLMClient,
    config: WikiConfig,
    logger: logging.Logger,
    log_fn: Callable[[str], None],
) -> None:
    import typer

    # Step 1: Empty-vault guard
    index_path = vault_root / "index.md"
    if not index_path.exists():
        typer.echo(
            "The vault has no summaries. Run 'codewiki ingest' first.", err=True
        )
        raise typer.Exit(code=1)

    # Step 2: Stale-row warning (informational only)
    stale_pages = _check_stale_rows(index_path)
    if stale_pages:
        count = len(stale_pages)
        names = ", ".join(stale_pages)
        typer.echo(
            f"⚠ {count} query page(s) are stale: {names} "
            f"— run codewiki lint to update."
        )

    # Step 3: Collect summary files
    summary_files = collect_summary_files(vault_root)
    logger.info("Found %d summary files for analysis", len(summary_files))

    if not summary_files:
        typer.echo("No summary files found. Run 'codewiki ingest' first.", err=True)
        raise typer.Exit(code=1)

    # Step 4: Build tiktoken batches (public function)
    batches = build_batches(summary_files, vault_root, logger)
    logger.info("Built %d batch(es) for analysis", len(batches))

    # Steps 5 & 6: Process each batch → partial overview → write per-dir overview.md
    partial_overviews: list[tuple[str, str]] = []   # (vault_dir, text)
    written_overview_paths: list[Path] = []

    for batch in batches:
        overview_text = _process_batch(batch, vault_root, llm_client, logger)
        partial_overviews.append((batch.vault_dir, overview_text))

        if batch.vault_dir:
            overview_path = vault_root / Path(batch.vault_dir) / "overview.md"
        else:
            overview_path = vault_root / "overview.md"
        written_overview_paths.append(overview_path)

    # Step 7: Synthesize root overview.md
    # If there's only one batch and it's at root level, it already wrote overview.md;
    # synthesis still runs (sending one partial to LLM for polish is fine).
    _synthesize_root_overview(partial_overviews, vault_root, llm_client, logger)
    root_overview_path = vault_root / "overview.md"
    # Ensure root overview.md is in the written list (synthesis always writes it)
    if root_overview_path not in written_overview_paths:
        written_overview_paths.append(root_overview_path)

    # Step 8: Update index.md
    _update_index_with_overviews(vault_root, written_overview_paths, logger)

    # Step 9: Log entry
    _write_analysis_log_entry(vault_root, len(summary_files), log_fn)

    typer.echo(
        f"Analysis complete. Reviewed {len(summary_files)} summaries across "
        f"{len(batches)} batch(es). Root overview.md written."
    )
```

---

### CLI Wiring — `analysis` Subcommand in `cli.py`

Add to `codebase_wiki_builder/cli.py`:

```python
@app.command()
def analysis(
    vault_path: Path = typer.Option(
        Path("."),
        "--vault", "-v",
        help="Path to the Obsidian vault root (default: current directory).",
    ),
) -> None:
    """Analyze wiki summaries and write overview.md."""
    from codebase_wiki_builder.analysis import run_analysis
    from codebase_wiki_builder.config import load_config
    from codebase_wiki_builder.llm_client import LLMClient, LLMError
    from codebase_wiki_builder.logging_setup import setup_logging, append_log_md
    from rich.console import Console

    vault_root = vault_path.resolve()
    if not vault_root.is_dir():
        typer.echo(f"Error: vault directory does not exist: {vault_root}", err=True)
        raise typer.Exit(code=1)

    logger = setup_logging(vault_root)
    log_fn = lambda entry: append_log_md(vault_root, entry)

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
```

---

## Error Handling

| Condition | Behavior |
|-----------|----------|
| `index.md` absent | Print error message matching FR-4; `raise typer.Exit(code=1)` |
| No summary files found (index.md exists but vault is empty) | Print informative message; `raise typer.Exit(code=1)` |
| Stale rows found in `index.md` | Print warning; continue normally |
| `LLMError` during any batch LLM call | Propagates from `_process_batch()`; CLI catches, logs at ERROR, `raise typer.Exit(code=1)` |
| `LLMError` during synthesis | Same as above — synthesis call also propagates `LLMError` |
| Individual summary file unreadable | Logged at WARNING; empty string used in place of content; batch continues |
| Batch exceeds window but cannot be subdivided | Logged at WARNING; batch sent anyway (best-effort) |
| `index.md` unwritable during overview row update | Logged at ERROR; overview files already written; no abort (partial success) |
| `overview.md` write fails (OSError) | `OSError` propagates from `write_text()`; CLI will print traceback. For MVP this is unhandled beyond propagation. |
| Vault root does not exist | Checked in CLI wiring before `setup_logging()`; `typer.Exit(code=1)` |
| `load_config()` invalid config | `load_config()` calls `sys.exit(1)` internally with informative message |

---

## Unit Test Specifications

**File**: `tests/test_analysis.py`

All tests use `tmp_path`. LLM calls are mocked via `unittest.mock.patch` or by passing a mock `LLMClient`. No real network calls.

---

### `_check_stale_rows()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| No stale rows | `index.md` with clean rows | Returns `[]` | No warning needed |
| One stale row | `index.md` with ` ⚠ stale` in one Description | Returns list with that page's path | FR-4: stale warning |
| Multiple stale rows | Two rows with ` ⚠ stale` | Returns both paths | Multiple pages |
| `index.md` does not exist | No file | Returns `[]` | Graceful |
| Stale in non-query row | Summary row with ` ⚠ stale` (edge case) | Included (function doesn't filter by page type) | Defensive |

---

### `collect_summary_files()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Empty vault | No `.md` files | Returns `[]` | No files |
| Summary in root | `vault/main.py.md` | Returns `("", path)` | Root-level file |
| Summary in subdir | `vault/src/auth/login.py.md` | Returns `("src/auth", path)` | Nested |
| `overview.md` excluded | `vault/overview.md` exists | Not in results | Special file |
| `index.md` excluded | `vault/index.md` exists | Not in results | Special file |
| `log.md` excluded | `vault/log.md` exists | Not in results | Special file |
| `logs/` dir excluded | `vault/logs/debug.log` | Not in results | Excluded dir |
| `queries/` dir excluded | `vault/queries/q.md` | Not in results | Excluded dir |
| Sub-dir `overview.md` excluded | `vault/src/overview.md` | Not in results | Special file |

---

### `_count_tokens()`

| Case | Input | Expected | Why |
|------|-------|----------|-----|
| Empty string | `""` | `0` | No tokens |
| Short string | `"hello world"` | Small positive integer | Basic token count |
| Returns int | Any string | `isinstance(result, int)` | Type contract |

---

### `build_batches()` — batching logic

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| All files fit in one group | 3 files in `src/`, combined tokens < 64000 | One batch with all 3 files | Happy path: single batch |
| Two top-level dirs, each fits | Files in `src/` and `tests/`, each < 64000 tokens | Two batches | Directory grouping |
| One dir too large → subdivided | `src/auth/` and `src/services/` both have files; `src/` combined > 64000 but each subdir < 64000 | Two batches: one per subdir | Subdivision |
| Single file too large | One file > 64000 tokens | One batch (with warning logged) | Cannot subdivide further |
| Root-level files | Files with `rel_dir == ""` | One batch with `vault_dir == ""` | Root group |
| Mixed root and dirs | Root files + `src/` files | Two batches: one root, one src | Separation by top-level dir |
| Empty file list | `[]` | `[]` (no batches) | Edge case |

**Key Scenario: Subdivision when directory exceeds window**

```python
def test_build_batches_subdivides_large_dir(tmp_path):
    from unittest.mock import patch
    from codebase_wiki_builder.analysis import build_batches
    import logging

    vault = tmp_path / "vault"
    vault.mkdir()

    # Create two subdirs under "src"
    auth_dir = vault / "src" / "auth"
    svc_dir = vault / "src" / "services"
    auth_dir.mkdir(parents=True)
    svc_dir.mkdir(parents=True)

    auth_file = auth_dir / "login.py.md"
    svc_file = svc_dir / "user.py.md"
    auth_file.write_text("auth content " * 100)
    svc_file.write_text("service content " * 100)

    summary_files = [
        ("src/auth", auth_file),
        ("src/services", svc_file),
    ]

    logger = logging.getLogger("test")

    # Patch _count_tokens: "src" combined → 70000 (over limit)
    # Each subdir alone → 30000 (under limit)
    call_count = [0]
    def mock_count_tokens(text: str, **kwargs) -> int:
        call_count[0] += 1
        # First call is for combined "src" content → over limit
        # Subsequent calls for subdirs → under limit
        if call_count[0] == 1:
            return 70_000
        return 30_000

    with patch("codebase_wiki_builder.analysis._count_tokens", side_effect=mock_count_tokens):
        batches = build_batches(summary_files, vault, logger)

    assert len(batches) == 2
    dirs = {b.vault_dir for b in batches}
    assert "src/auth" in dirs
    assert "src/services" in dirs
```

---

### `_process_batch()` — LLM call and file write

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Successful batch | Mock `llm_client.complete` returns overview text | Returns overview text; `overview.md` written at batch dir | Happy path |
| `LLMError` raised | Mock `llm_client.complete` raises `LLMError` | `LLMError` propagates | CLI catches for exit 1 |
| Batch at root (`vault_dir=""`) | Batch with empty `vault_dir` | `overview.md` written at `vault_root/overview.md` | Root batch |
| Subdir batch (`vault_dir="src/auth"`) | Batch with `vault_dir="src/auth"` | `overview.md` written at `vault_root/src/auth/overview.md` | Subdir batch |
| `overview.md` overwritten | Existing `overview.md` in dir | File overwritten with new content | FR-4: overwrite if exists |

---

### `_synthesize_root_overview()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Single partial overview | One partial | LLM called with synthesis prompt; `overview.md` written | Minimal case |
| Multiple partials | Three partials | LLM receives all three in prompt; root `overview.md` written | Standard case |
| `LLMError` | Mock raises `LLMError` | Propagates | CLI catches |
| `overview.md` overwritten | Existing root `overview.md` | File overwritten | FR-4: overwrite if exists |

---

### `_update_index_with_overviews()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| New overview, no prior row | `index.md` exists with no overview row | New row appended for overview | First analysis |
| Existing overview row updated | `index.md` has existing root overview row | Row replaced with current description | Idempotent on re-run |
| Root overview row description | Root `overview.md` | Description = "Top-level application overview" | FR-4 |
| Subdir overview row description | `src/auth/overview.md` | Description = "Directory overview: src/auth/" | FR-4 |
| `index.md` absent | No index.md | Logs WARNING; no crash | Defensive |

---

### `run_analysis()` — integration

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Empty vault (no index.md) | No `index.md` in vault | `typer.Exit(code=1)` raised; error message printed | FR-4: hard error |
| No summary files (index.md exists) | `index.md` exists but no `.md` summaries | `typer.Exit(code=1)` raised | No files to analyze |
| Stale rows → warning printed | `index.md` with stale row; summaries exist | Warning printed; analysis proceeds | FR-4: informational only |
| Successful run | 3 summary files; mock LLM | Exit 0; `overview.md` written; `index.md` updated; `log.md` entry appended | AT-6 |
| Log entry format | Any successful run | `log.md` contains entry matching `YYYY-MM-DD HH:MM:SS UTC \| analysis \| summaries_reviewed=N` | FR-6.1 |
| LLM error during batch | Mock `llm_client.complete` raises `LLMError` on second call | `LLMError` propagates; CLI converts to exit 1 | FR-4: fatal API error |

**Key Scenario: Successful analysis end-to-end**

```python
def test_run_analysis_happy_path(tmp_path):
    from unittest.mock import MagicMock, patch
    from pathlib import Path
    import json
    import logging
    from codebase_wiki_builder.analysis import run_analysis

    vault = tmp_path / "vault"
    vault.mkdir()
    src_dir = vault / "src"
    src_dir.mkdir()

    # Create index.md
    (vault / "index.md").write_text(
        "| File | Description |\n|------|-------------|\n"
        "| [[src/main.py]] | Main module |\n"
    )

    # Create summary file
    (src_dir / "main.py.md").write_text(
        "# src/main.py\n\nEntry point.\n\n<!-- md5: abc123 -->\n"
    )

    mock_llm = MagicMock()
    mock_llm.complete.return_value = "This directory handles the main entry point."

    mock_config = MagicMock()
    logger = logging.getLogger("test")
    log_entries = []

    with patch("codebase_wiki_builder.analysis._count_tokens", return_value=100):
        run_analysis(vault, mock_llm, mock_config, logger, log_entries.append)

    # root overview.md written
    assert (vault / "overview.md").exists()
    assert "entry point" in (vault / "overview.md").read_text()

    # log.md entry appended
    assert any("analysis" in e and "summaries_reviewed=1" in e for e in log_entries)

    # index.md updated with overview row
    index_content = (vault / "index.md").read_text()
    assert "overview" in index_content.lower()
```

---

### CLI `analysis` subcommand

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| No vault dir | `--vault /nonexistent` | Exit code 1; error about missing dir | Guard |
| Invalid config | Bad `.wiki-config.json` | Exit code 1 (from `load_config()`) | Config validation |
| Missing API key | Valid config; no `.env` key | Exit code 1; `LLMError` message | LLMClient construction |
| Successful run | Valid config + mock LLM | Exit code 0; `overview.md` exists | Happy path |
| `run_analysis` raises `LLMError` | Mock raises during batch | Exit code 1; error printed | Fatal LLM error |

---

## Notes

- **`ANALYSIS_CONTEXT_WINDOW`, `build_batches`, and `collect_summary_files` are imported by item 16 (lint health-check)**: Item 16 uses the same token-budget constant and identical batching strategy. These are public functions and constants in `analysis.py`; item 16 imports them directly rather than re-defining the logic.

- **tiktoken encoding choice (`cl100k_base`)**: This encoding is used by the GPT-4 family (OpenAI) and is widely accepted as a good-enough approximation for Anthropic models. The spec uses tiktoken for estimation (not exact accounting), so slight over- or under-estimation is acceptable. The 64,000-token window provides substantial headroom.

- **Caching the tiktoken encoder**: For large vaults with hundreds of files, calling `tiktoken.get_encoding()` once per file is wasteful. A module-level lazy-cached encoder would be more efficient. For MVP with expected vault sizes, per-call is acceptable. The `_count_tokens()` helper can be upgraded to use a module-level cached encoder without changing its interface.

- **Root-level files edge case**: Files at the vault root (e.g., `vault/README.md.md`) have `rel_dir == ""`. These are grouped into a single root batch. Their partial overview is written to `vault_root / "overview.md"`, which will immediately be overwritten by the synthesis step. This is acceptable: the synthesis step's output is the definitive root `overview.md`.

- **Single-batch vault (entire codebase fits in one batch)**: If all summaries fit within 64,000 tokens, there is one batch at the top level. The synthesis step is still called (with one partial overview input). The result is a synthesis of one partial — which is functionally equivalent to just using the partial directly. This is a no-op semantically but keeps the code path uniform.

- **`_process_batch()` for root files writes `overview.md` at vault root**: The synthesis step then overwrites this with the synthesized result. The intermediate write is wasted I/O but not incorrect — the synthesis call always produces the definitive root `overview.md`.

- **`_update_index_with_overviews()` does not call `rebuild_index()`**: It performs targeted row updates/appends to avoid the full vault walk that `rebuild_index()` requires. This is intentional — `run_analysis()` runs after ingest and should not disturb existing index rows for summary files and query pages. Only overview rows are added or updated.

- **`LLMError` propagation**: `run_analysis()` does not catch `LLMError`. It propagates to the CLI `analysis` command, which catches it, logs at ERROR, and calls `raise typer.Exit(code=1)`. This keeps `analysis.py` free of CLI concerns.

- **No `ChangeSet` dependency**: Unlike the ingest pipeline, `run_analysis()` has no dependency on `scanner.py` or `ChangeSet`. It reads directly from the vault's current state. This makes it independently runnable after any number of ingest operations.

- **`dataclasses` import**: `AnalysisBatch` uses `@dataclass`. Add `from dataclasses import dataclass` at the top of `analysis.py`.

- **Test mocking strategy for `_count_tokens()`**: Tests should patch `codebase_wiki_builder.analysis._count_tokens` to return controlled values. This avoids the need for tiktoken to be installed during unit tests and makes batch-boundary tests deterministic.
