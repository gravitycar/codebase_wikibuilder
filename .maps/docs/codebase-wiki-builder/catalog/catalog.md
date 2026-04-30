# Implementation Catalog: Codebase Wiki Builder

## Overview

This catalog decomposes the Codebase Wiki Builder into discrete, buildable items. Each item covers at most ~3 files and can be implemented in a single plan. Items are ordered by dependency: items with no `Blocked by` entry can be started immediately; items listing blockers must wait for those to complete first.

The application package root is `codebase_wiki_builder/` (importable as `codebase_wiki_builder`), installed via `uv` + `pyproject.toml`.

---

## Catalog Items

---

### 1. Project Scaffold and Package Manifest

- **Purpose**: Establish the installable Python package, declare all runtime dependencies, configure Ruff/mypy/pytest, and define the two CLI entry points (`codewiki` and `wiki-mcp`).
- **Scope**:
  - `pyproject.toml` — project metadata, dependencies, `[project.scripts]`, `[tool.ruff]`, `[tool.mypy]`, `[tool.pytest.ini_options]`
  - `codebase_wiki_builder/__init__.py` — package marker (empty or version string)
- **Blocks**: All other items (everything depends on the package existing)
- **Blocked by**: None
- **Acceptance Criteria**: FR-1 (entry points), Technical Context (pyproject.toml structure, dependency list)

---

### 2. Configuration Model and Loader

- **Purpose**: Implement the `.wiki-config.json` reader/writer and `.env` loader. Validates all fields on load; prompts interactively for the codebase path on first `ingest` if the config file is absent.
- **Scope**:
  - `codebase_wiki_builder/config.py` — `WikiConfig` dataclass/pydantic model, `load_config()`, `save_config()`, `prompt_for_config()`, validation logic, `.env` loading via `python-dotenv`
- **Blocks**: 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13
- **Blocked by**: 1
- **Acceptance Criteria**: FR-2 (all sub-requirements: config fields, validation, error messages, exit code 1 on invalid config, secrets in `.env` only)

---

### 3. LLM Client Abstraction

- **Purpose**: Implement a thin provider-agnostic LLM client that routes to the `anthropic` or `openai` SDK based on the config. Handles rate-limit retry with exponential backoff (up to 5 attempts, max 30 s, with jitter). Inter-request delay is enforced here.
- **Scope**:
  - `codebase_wiki_builder/llm_client.py` — `LLMClient` class with `complete(prompt: str) -> str`, retry logic, provider routing, backoff constants
- **Blocks**: 5, 6, 7, 8, 12
- **Blocked by**: 1, 2
- **Acceptance Criteria**: FR-3.4 (retry/backoff, non-retriable error handling, exit code 1 on exhaustion), FR-2 (provider/model config), Technical Context (Anthropic SDK primary, OpenAI optional)

---

### 4. Vault File Utilities and Logging Infrastructure

- **Purpose**: Provide shared filesystem helpers (path mirroring, slug generation, MD5 computation, wikilink formatting) and implement both logging sinks: the append-only `log.md` human log and the per-run debug log under `logs/`.
- **Scope**:
  - `codebase_wiki_builder/vault.py` — path mirror logic, slug generation, MD5 hashing, wikilink formatting, binary-file detection
  - `codebase_wiki_builder/logging_setup.py` — `setup_logging()` (creates `logs/YYYY-MM-DD_HH-MM-SS.log`), `append_log_md(entry: str)` (appends to `log.md`, never truncates)
- **Blocks**: 5, 6, 7, 8, 9, 10, 11, 12, 13
- **Blocked by**: 1
- **Acceptance Criteria**: FR-3.2 (binary detection, excluded dirs), FR-3.3 (MD5), FR-3.5 (naming convention `<name>.<ext>.md`), FR-6.1 (log.md format, append-only), FR-6.2 (debug log path/format)

---

### 5. File Discovery and Change-Set Computation (Ingest Phase 1)

- **Purpose**: Implement the ingest Phase 1 scanner: recursively discover eligible source files (applying exclusion rules), compute MD5 hashes, compare against stored hashes in existing summary footers, and identify deleted summaries. Produces a complete change-set (new, modified, deleted) without writing anything to the vault.
- **Scope**:
  - `codebase_wiki_builder/scanner.py` — `scan_codebase()` returning a `ChangeSet` dataclass with new/modified/deleted file sets; reads existing vault summary footers to extract stored MD5s; applies size-threshold, binary, and excluded-directory filters
- **Blocks**: 6
- **Blocked by**: 2, 4
- **Acceptance Criteria**: FR-3.1 (directory mirroring), FR-3.2 (discovery/filtering), FR-3.3 (change detection), FR-3.7 (deleted file detection in Phase 1), FR-3 preamble (Phase 1 produces change-set, no vault writes)

---

### 6. Summarization and Summary File Writer (Ingest Phase 2 — Core)

- **Purpose**: For each new/modified file in the change-set, send its content to the LLM (via `LLMClient`) using the summarization prompt (description + class/module sections + explicit/dynamic references), validate returned reference paths against the real file tree, and write the structured summary file (title → description → `## References` → MD5 footer).
- **Scope**:
  - `codebase_wiki_builder/summarizer.py` — `summarize_file(path, llm_client, config) -> str` (prompt construction, LLM call, reference validation, summary string assembly); `write_summary(vault_path, summary_str)`
- **Blocks**: 7
- **Blocked by**: 3, 4, 5
- **Acceptance Criteria**: FR-3.4 (summarization prompt, inter-request delay), FR-3.5 (summary file format: title, description, `## References` with `(inferred)` annotation, MD5 footer `<!-- md5: ... -->`)

---

### 7. Deletion Handling and Backlink Cleanup (Ingest Phase 2 — Deletions)

- **Purpose**: For each deleted entry in the change-set, delete the summary file from the vault, scan all remaining summary files for backlinks to the deleted path, and remove those backlinks. Log each deletion and backlink removal.
- **Scope**:
  - `codebase_wiki_builder/deletion.py` — `apply_deletions(change_set, vault_root, log_fn)`: deletes summary files, O(n) backlink scan, removes dead backlinks, cleans up empty vault directories
- **Blocks**: 8
- **Blocked by**: 4, 5, 6
- **Acceptance Criteria**: FR-3.7 (deletion and backlink cleanup in Phase 2), FR-3.1 (empty directory cleanup)

---

### 8. Index Regeneration (`index.md`) and Staleness Detection

- **Purpose**: After Phase 2 summary writes and deletions, regenerate `index.md` as a two-column markdown table covering all summary files and all `queries/` pages. Then run staleness detection: for each `queries/` page, parse its `## Sources` section and compare against the Phase 1 change-set; insert or detect existing stale banners; annotate `index.md` rows; log `query-stale` entries; report stale pages to the terminal.
- **Scope**:
  - `codebase_wiki_builder/index_writer.py` — `rebuild_index(vault_root) -> None` (reads summary files + queries/, writes two-column table to `index.md`)
  - `codebase_wiki_builder/staleness.py` — `detect_stale_queries(change_set, vault_root, log_fn) -> list[str]`: parses `## Sources`, checks for existing banner, inserts banner after H1, annotates index, logs; hard-error on missing/malformed `## Sources`
- **Blocks**: 9, 10, 11, 12
- **Blocked by**: 4, 5, 7
- **Acceptance Criteria**: FR-3.6 (index format, complete rebuild each ingest, query pages preserved), FR-3.8 (staleness detection: Phase 1 change-set including deleted paths, banner placement after H1, duplicate-banner prevention, `## Sources` hard error, `query-stale` log, terminal summary)

---

### 9. Ingest Command — CLI Wiring

- **Purpose**: Wire the `ingest` subcommand in the Typer app. Orchestrates Phase 1 (scanner), Phase 2 (summarizer, deletion, index, staleness), first-run config prompting, progress display via `rich`, and the completion summary. Handles exit codes (0, 1, 2).
- **Scope**:
  - `codebase_wiki_builder/cli.py` — Typer app definition, `ingest` subcommand; `codewiki` entry point; progress/summary display
- **Blocks**: 13 (CLI app must exist before other commands are added), 15 (MCP server imports from core modules also used by CLI)
- **Blocked by**: 2, 4, 5, 6, 7, 8
- **Acceptance Criteria**: FR-1 (CLI entry point, progress display, completion summary), FR-3 preamble (mandatory two-phase execution), FR-2 (first-run interactive prompt), exit codes 0/1/2

---

### 10. Analysis Command

- **Purpose**: Implement the `analysis` subcommand. Reads all summary files, batches them by directory tree using `tiktoken` (ANALYSIS_CONTEXT_WINDOW = 64,000 tokens), sends each batch to the LLM, writes per-directory `overview.md` files, synthesizes a unified root `overview.md`, updates `index.md`, and appends a log entry. Prints stale-page warning at startup if any rows are flagged.
- **Scope**:
  - `codebase_wiki_builder/analysis.py` — `run_analysis(vault_root, llm_client, config)`: tiktoken batching by directory, partial overview generation, subdirectory `overview.md` writes, final synthesis, root `overview.md` write, index update, log append, stale-warning check
- **Blocks**: None (standalone feature)
- **Blocked by**: 3, 4, 8, 9
- **Acceptance Criteria**: FR-4 (all: stale warning, empty-vault error, tiktoken batching strategy with ANALYSIS_CONTEXT_WINDOW, subdirectory overview files, root overview.md, index update, log entry)

---

### 11. Query Core Logic

- **Purpose**: Implement the shared query workflow used by both the CLI `query` command and the MCP server: read `index.md`, identify relevant summaries via LLM (JSON array sorted by relevance), fill context budget using `tiktoken` (QUERY_CONTEXT_WINDOW = 128,000 tokens), send question + summaries to LLM (returns answer + one-line summary), handle oversized files and budget overflow, format `## Sources` section.
- **Scope**:
  - `codebase_wiki_builder/query_engine.py` — `run_query(question: str, vault_root, llm_client, config) -> QueryResult` (dataclass with answer, sources, one_line_summary, stale_warnings); all token-budget logic, relevance sorting, sources annotation
- **Blocks**: 12, 13, 15
- **Blocked by**: 3, 4, 8
- **Acceptance Criteria**: FR-5 (relevance identification via JSON array sorted by relevance, token budget filling from top of list, oversized-file skip, overflow note, `## Sources` section, stale warning check at start), Technical Context (QUERY_CONTEXT_WINDOW constant)

---

### 12. Query Page Persistence and Slug Management

- **Purpose**: Implement saving a query result to `queries/<slug>.md` (with numeric suffix deduplication), constructing the saved page format (H1 title, answer body, `## Sources`, `## Page Metadata` with `saved_at`/`updated_at`), updating `index.md` with the LLM-generated one-line description, and appending `query-saved` to `log.md`.
- **Scope**:
  - `codebase_wiki_builder/query_persistence.py` — `save_query_page(question, result, vault_root, log_fn) -> Path`: slug generation, numeric-suffix deduplication, page file write, `index.md` row append, log entry; `read_query_page(path) -> QueryPage` (for lint use)
- **Blocks**: 13, 14
- **Blocked by**: 4, 8, 11
- **Acceptance Criteria**: FR-5 (save prompt flow, slug logic, numeric suffix, no-overwrite rule, `## Sources` in saved page, `## Page Metadata` with `saved_at`/`updated_at`, `query-saved` log, index row with LLM-generated description), FR-3.8 (pages must have well-formed `## Sources` for staleness to work)

---

### 13. Query Command — CLI Wiring

- **Purpose**: Wire the `query` subcommand in the Typer app. Calls `run_query`, prints the answer, prompts the user to save (default No), calls `save_query_page` on `y`/`Y`, handles exit codes (0, 3). Prints stale-page warning before answering.
- **Scope**:
  - `codebase_wiki_builder/cli.py` — `query` subcommand (added to existing Typer app)
- **Blocks**: None (standalone feature once wired)
- **Blocked by**: 9, 11, 12
- **Acceptance Criteria**: FR-5 (CLI-specific: interactive save prompt, default-No behavior, exit code 3 on zero relevant files, exit code 0 on normal completion)

---

### 14. Lint — Part 1: Staleness Resolution

- **Purpose**: Implement lint Part 1: read `index.md` for stale rows, strip all stale banners from each affected page, re-run the full query workflow internally (logged as `lint-query`, no standard query log entry), handle the unknowable case (zero relevant files — insert unknowable banner, update index, log `lint-unknowable`), overwrite pages with fresh answers, update `updated_at`, clean index annotations, log `lint-resolved`, print per-page terminal output.
- **Scope**:
  - `codebase_wiki_builder/lint_staleness.py` — `resolve_stale_pages(vault_root, llm_client, config, log_fn) -> LintStalenessResult`: banner stripping, internal query re-run, unknowable handling, page overwrite, index update, logging
- **Blocks**: 16
- **Blocked by**: 9, 11, 12
- **Acceptance Criteria**: FR-8.1 (all steps: banner strip, re-run, unknowable branch, page overwrite, `updated_at` update, `saved_at` preserved, index annotation removal, `lint-query` log, `lint-resolved` log, `lint-unknowable` log, terminal output, no abort on unknowable)

---

### 15. MCP Server

- **Purpose**: Implement the `wiki-mcp` entry point: an MCP stdio server exposing exactly one tool (`wiki_query`). Reuses `run_query` and `save_query_page` from core modules. Always saves automatically (no save prompt). Returns structured JSON (`answer`, `sources`, `saved_path`, `stale_warning` as `list[str]|null`). Rejects unknown parameters (e.g., `save`). No rich terminal output; errors as MCP error responses.
- **Scope**:
  - `codebase_wiki_builder/mcp_server.py` — `main()` entry point, MCP stdio transport setup, `wiki_query` tool handler, structured JSON response assembly, unknown-parameter rejection
- **Blocks**: None (standalone transport layer)
- **Blocked by**: 2, 11, 12
- **Acceptance Criteria**: FR-9 (all: `wiki-mcp` entry point, stdio transport, `wiki_query` tool schema, auto-save, `saved_path` always present, `stale_warning` as `list[str]|null`, no `save` parameter, no rich output, errors as MCP error responses, shared core logic), FR-9.3, FR-9.4

---

### 16. Lint — Part 2: Semantic Deduplication and Part 3: Deep Health-Check

- **Purpose**: Implement lint Part 2 (semantic deduplication: LLM-based near-duplicate detection using titles/descriptions only, merge via LLM, preserve `saved_at`/update `updated_at`, delete merged pages, update index, log `lint-deduplicated`) and Part 3 (deep health-check: tiktoken-batched LLM analysis of actual summary content plus `index.md`, four-category findings, final synthesis, write `lint-report.md`).
- **Scope**:
  - `codebase_wiki_builder/lint_dedup.py` — `deduplicate_query_pages(vault_root, llm_client, log_fn) -> LintDedupResult`: LLM detection pass (titles/descriptions only), full-content merge, file operations, index update, logging
  - `codebase_wiki_builder/lint_healthcheck.py` — `run_health_check(vault_root, llm_client, log_fn) -> None`: tiktoken batching (ANALYSIS_CONTEXT_WINDOW), four-category LLM analysis, synthesis, `lint-report.md` write
- **Blocks**: 17
- **Blocked by**: 4, 8, 14
- **Acceptance Criteria**: FR-8.2 (conservative deduplication threshold, detection using titles/descriptions only, merge recency by `updated_at`, numeric-suffix slug for output, `saved_at` preserved, `updated_at` updated, `lint-deduplicated` log, terminal output), FR-8.3 (same tiktoken batch strategy as analysis, `index.md` in every batch, four sections, synthesis step, `lint-report.md` format)

---

### 17. Lint Command — CLI Wiring and Help Command

- **Purpose**: Wire the `lint` subcommand in the Typer app (orchestrates Parts 1, 2, 3 in sequence; empty-vault guard; exit codes 0/1). Also implement the `help` subcommand with all three forms: no-arg overview table, per-command detail pages (ingest/analysis/query/lint/mcp), and `help mcp` with runtime-resolved vault path and `.mcp.json` snippet.
- **Scope**:
  - `codebase_wiki_builder/cli.py` — `lint` subcommand and `help` subcommand (added to existing Typer app)
- **Blocks**: None (final CLI assembly)
- **Blocked by**: 9, 14, 16
- **Acceptance Criteria**: FR-8 (lint CLI orchestration, empty-vault error, exit codes), FR-10 (help overview table, per-command detail pages, `help mcp` with resolved vault path and `.mcp.json` snippet, unrecognized-arg error + general help + exit 0)

---

### 18. Obsidian CLI Integration (Optional / Exploratory)

- **Purpose**: Attempt to enable the Obsidian Search core plugin via the Obsidian CLI on startup. Must degrade gracefully if Obsidian is not installed, not running, or does not respond within 5 seconds. Failures are logged as warnings, never errors.
- **Scope**:
  - `codebase_wiki_builder/obsidian_cli.py` — `try_enable_search_plugin(log_fn)`: subprocess invocation with 5-second timeout, warning-level error handling
- **Blocks**: None
- **Blocked by**: 1, 4
- **Acceptance Criteria**: FR-7 (best-effort, graceful degradation, 5-second timeout, warning log on failure, no blocking of other operations)

---

## Dependency Summary

```
1 (Scaffold)
├── 2 (Config)
│   ├── 3 (LLM Client)
│   │   ├── 6 (Summarizer)  ← also needs 4, 5
│   │   ├── 10 (Analysis)   ← also needs 4, 8, 9
│   │   ├── 11 (Query Core) ← also needs 4, 8
│   │   └── 15 (MCP Server) ← also needs 11, 12
│   └── 9 (Ingest CLI)      ← also needs 4, 5, 6, 7, 8
└── 4 (Vault Utils + Logging)
    ├── 5 (Scanner / Phase 1)  ← also needs 2
    │   ├── 6 (Summarizer)     ← also needs 3
    │   │   └── 7 (Deletions)  ← also needs 4, 5
    │   │       └── 8 (Index + Staleness)
    │   │           ├── 9 (Ingest CLI)     ← also needs 2, 5, 6, 7
    │   │           ├── 10 (Analysis CLI)  ← also needs 3, 9
    │   │           ├── 11 (Query Core)    ← also needs 3
    │   │           │   └── 12 (Query Persistence) ← also needs 4, 8
    │   │           │       ├── 13 (Query CLI) ← also needs 9, 11
    │   │           │       ├── 14 (Lint P1)   ← also needs 9, 11
    │   │           │       └── 15 (MCP Server)← also needs 2, 11
    │   │           └── 16 (Lint P2+P3)  ← also needs 4, 8, 14
    │   │               └── 17 (Lint CLI + Help) ← also needs 9, 14, 16
    └── 18 (Obsidian CLI) — independent optional item
```

**Suggested build order (linear)**: 1 → 2 → 4 → 3 → 5 → 6 → 7 → 8 → 9 → 11 → 12 → 10 → 13 → 14 → 15 → 16 → 17 → 18

Items 10, 13, 15 can be parallelized once their blockers are met. Item 18 can be built any time after items 1 and 4.
