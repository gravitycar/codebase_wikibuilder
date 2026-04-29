# Codebase Wiki Builder Specification

## Document Metadata
- **Version**: 1.8.0
- **Author**: Architect Agent (MAPS)
- **Created**: 2026-04-29
- **Status**: Draft
- **Related Specs**: None (initial specification)

---

## Executive Summary

The Codebase Wiki Builder is a Python CLI tool that scans a target codebase, uses an LLM to generate per-file markdown summaries, and writes those summaries into a structured Obsidian vault. It supports incremental updates (MD5-based change detection), handles deletions with backlink cleanup, produces a catalog (`index.md`) and analysis overview (`overview.md`), and exposes four commands — `ingest`, `analysis`, `query`, and `lint` — to manage and interrogate the wiki. The `ingest` command uses a mandatory two-phase approach (compute change-set first, then apply) and detects stale saved query pages after file changes; `lint` resolves staleness, flags unanswerable pages as "unknowable", and performs a deep health-check of the vault.

---

## User Story

As a developer using LLM coding agents, I want a persistent, human-readable wiki of my codebase stored in an Obsidian vault, so that I can give an LLM a compact, accurate understanding of the codebase without re-scanning every source file on every session.

---

## Stakeholders

- **Primary**: Individual developers running LLM-assisted coding workflows
- **Secondary**: LLM coding agents that consume the wiki as context
- **Tertiary**: None (local single-user tool, no operational team)

---

## Success Criteria

1. **Ingest completeness**: After `ingest` completes, every non-binary, non-oversized file in the target codebase has a corresponding up-to-date summary in the vault.
2. **Incremental correctness**: On re-run, `ingest` skips unchanged files (same MD5) and only re-summarizes files whose content has changed.
3. **Deletion handling**: If a source file is deleted from the target codebase, its summary is removed from the vault AND all other summaries that contain backlinks to it are updated to remove those backlinks.
4. **Catalog accuracy**: `index.md` lists every current summary with a link and one-line description; it is updated on every `ingest` run.
5. **Analysis output**: `analysis` produces a non-empty `overview.md` covering the codebase's apparent purpose, dominant patterns, and notable observations.
6. **Query grounding**: `query` returns an answer grounded in `index.md` and the summaries, not generic knowledge. The answer cites which summaries were consulted.
7. **First-run setup**: On first `ingest` when no config file exists, the user is prompted for the target codebase path; that path is persisted to `.wiki-config.json` for all future runs.
8. **Operational log**: Every `ingest`, `analysis`, and `query` operation appends a timestamped entry to `log.md` and writes a dated debug log under `logs/`.

---

## Context and Problem Statement

### Current State

When an LLM coding agent needs to understand an existing codebase, it typically reads source files directly — consuming large context windows, repeating work every session, and lacking cross-file awareness. There is no persistent, compact representation of the codebase that an agent can consult.

### Desired State

An Obsidian vault containing one markdown summary per source file, cross-linked with Obsidian backlinks, accompanied by a catalog (`index.md`) and analysis overview (`overview.md`). The wiki is incrementally maintained: only changed files are re-summarized. An LLM agent can orient itself quickly by reading `index.md` first, then drilling into relevant summaries.

---

## Functional Requirements

### FR-1: CLI Interface and Entry Point

- The application SHALL be installed as two standalone CLI commands via `[project.scripts]` in `pyproject.toml`: `codewiki` (the CLI entry point) and `wiki-mcp` (the MCP server entry point, defined in FR-9).
- The CLI SHALL expose five subcommands: `ingest`, `analysis`, `query`, `lint`, and `help`.
- The CLI SHALL always be invoked from the root of the target Obsidian vault. The vault root is the current working directory at runtime.
- The CLI SHALL display progress information (file counts, current file, errors) during `ingest` using a styled terminal output.
- The CLI SHALL print a human-readable summary upon completion of each command (files processed, skipped, errors, etc.).

### FR-2: Configuration

- On startup, the application SHALL look for `.wiki-config.json` in the current working directory (vault root).
- If `.wiki-config.json` does not exist and the user runs `ingest`, the application SHALL interactively prompt the user for the absolute path to the target codebase, validate that it is a readable directory, and write the config file before proceeding.
- If `.wiki-config.json` exists but contains invalid data (malformed JSON, a codebase path that does not exist or is not a readable directory, or invalid field values such as a negative inter-request delay), the application SHALL exit with code 1 and print an informative error message that includes: the config file path, the offending field name, and the expected format. The application SHALL NOT proceed with any operation when config validation fails.
- `.wiki-config.json` SHALL store: target codebase path, LLM provider (default: `anthropic`), LLM model (default: `claude-sonnet-4-6`), file size threshold in bytes (default: 100,000), and inter-request delay in seconds (default: 1.0).
- LLM API keys SHALL be stored only in a `.env` file (vault root) and never in `.wiki-config.json` or source code. Supported keys: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`.
- The `.env` file SHALL be loaded automatically at runtime.
- The application SHALL support two LLM providers selectable via config: `anthropic` (default) and `openai`. The provider and model are read from `.wiki-config.json` at startup.

### FR-3: Ingest Command

The `ingest` command scans the target codebase, detects changed/new/deleted files, and updates the vault accordingly. Ingest SHALL use a mandatory two-phase approach:

- **Phase 1 — Change-set computation**: Before making any changes to the vault, compute the full change-set: the set of source files that are new, modified (MD5 mismatch), or deleted (summary exists but source file does not). No vault files are created, modified, or deleted during Phase 1.
- **Phase 2 — Apply changes**: Apply all changes using the Phase 1 change-set as input. Summary files are written for new/modified source files; summary files for deleted source files are removed; backlinks are cleaned up; staleness detection runs using the complete change-set (including deleted summary paths) computed in Phase 1.

This two-phase approach ensures that deleted summary paths are known before any deletions occur, so that FR-3.8 staleness detection can correctly flag query pages that reference deleted summaries.

#### FR-3.1: Directory Mirroring

- The application SHALL mirror the target codebase's directory structure inside the vault root. For each directory in the target codebase, a corresponding directory SHALL exist in the vault.
- Directories that no longer exist in the target codebase SHALL have their vault counterparts cleaned up (only if they become empty after summary removal; non-empty directories are left intact).

#### FR-3.2: File Discovery and Filtering

- The application SHALL recursively scan the target codebase for all files.
- Binary files SHALL be excluded from summarization. A file is considered binary if it contains null bytes or cannot be decoded as UTF-8 text. Common binary extensions (`.png`, `.jpg`, `.jpeg`, `.gif`, `.bmp`, `.ico`, `.svg`, `.pdf`, `.zip`, `.tar`, `.gz`, `.exe`, `.dll`, `.so`, `.pyc`, `.class`, `.wasm`) SHALL also be excluded.
- Files whose size exceeds the configured threshold SHALL be skipped. Skipped files SHALL be logged in the operational log with a warning, and their skip status SHALL be noted in `log.md`.
- Files within `.git/`, `.venv/`, `node_modules/`, `__pycache__/`, and `.maps/` directories SHALL be excluded from scanning.

#### FR-3.3: Change Detection

- For each eligible file in the target codebase, the application SHALL compute an MD5 hash of its contents.
- If a corresponding summary file exists in the vault AND that summary file contains a matching MD5 hash in its footer, the file is considered unchanged and SHALL be skipped.
- If no corresponding summary exists, or the stored MD5 does not match the current hash, the file SHALL be (re-)summarized.

#### FR-3.4: LLM Summarization

- The application SHALL send the full text of each eligible changed/new file to the configured LLM for summarization.
- The LLM prompt SHALL instruct the model to produce a structured markdown summary containing:
  - A brief description of the file's purpose.
  - If the file defines one or more classes or modules: a section for each class/module with brief summaries of its properties and methods.
  - Otherwise: a plain prose description of what the file does.
- The application SHALL wait at least the configured inter-request delay between successive LLM API calls.
- On an HTTP 429 (rate limit) response, the application SHALL retry with exponential backoff: initial wait 1 second, doubling each retry up to a maximum of 30 seconds, with random jitter. The application SHALL retry up to 5 times; if all 5 retries are exhausted, the application SHALL log the error and exit with code 1.
- On any non-retriable API error (e.g., authentication failure, invalid request), the application SHALL log the error and exit with code 1.

#### FR-3.5: Summary File Format

Each summary file SHALL be written as a markdown file at the vault path that mirrors the target file's path using the naming convention `<name>.<ext>.md`. For example: `src/services/user_service.py` in the target codebase becomes `src/services/user_service.py.md` in the vault. The source file extension is always preserved; `.md` is appended as an additional extension.

The summary file SHALL contain the following sections in order:

1. **Title**: The target file's relative path from the target codebase root.
2. **Description / class-module section**: LLM-generated summary content as described in FR-3.4.
3. **Backlinks section** (heading: `## References`): A list of Obsidian wikilinks (`[[relative/path/to/file]]`) to other files in the target codebase that reference the summarized file. References are populated as follows:
   - The LLM summarization prompt (FR-3.4) SHALL instruct the model to return a structured list of files that reference the file being summarized, split into two categories:
     - **Explicit references**: imports, requires, includes, or any other static, syntactic references to the file.
     - **Dynamic references**: any patterns suggesting runtime file loading (e.g., dynamic imports, plugin loaders, string-based path construction), each accompanied by a brief explanation of why the reference is suspected.
   - The application SHALL cross-reference the LLM-returned list against the actual file tree to resolve entries to real, existing paths. Paths that do not resolve to a real file SHALL be discarded.
   - Dynamic references that resolve to a real file SHALL be included in the `## References` section annotated with `(inferred)` — e.g., `[[src/plugins/loader.py]] (inferred)`.
4. **Hash footer**: A final line in the format `<!-- md5: <hexdigest> -->` containing the MD5 hash of the source file at the time of summarization.

#### FR-3.6: Index Update

- After all summaries are written, the application SHALL regenerate `index.md` in the vault root.
- `index.md` SHALL contain a markdown TABLE (not a list) of **all current wiki pages**: both summary files (generated by `ingest`) and saved query pages (generated by `query` or `lint`, stored under `queries/`). The table SHALL have exactly two columns: **File** (an Obsidian wikilink to the page) and **Description** (the one-line description for that page).
- `index.md` SHALL be completely rewritten on each ingest (not appended to). When rewriting, the application SHALL read both the vault's summary files and all files under `queries/` to reconstruct the full table, so that query pages are never lost on subsequent ingest runs.

#### FR-3.7: Deleted File Handling

Deleted file handling runs during Phase 1 (detection) and Phase 2 (application) as described in the FR-3 preamble.

- During Phase 1, the application SHALL build a set of all source files that currently exist in the target codebase. For each summary file in the vault (excluding `index.md`, `log.md`, `overview.md`, any `overview.md` file in any subdirectory, files under `logs/`, and files under `queries/`), the application SHALL check whether the corresponding source file still exists. Summary files whose source no longer exists are added to the Phase 1 change-set as deleted entries.
- During Phase 2, for each summary file marked as deleted in the Phase 1 change-set:
  - The summary file SHALL be deleted from the vault.
  - All other summary files in the vault SHALL be scanned for backlinks referencing the deleted summary. Any such backlinks SHALL be removed from those files. This is an O(n) scan across all summaries and SHALL be performed as part of every ingest that involves deletions.
  - The deletion and backlink cleanup SHALL be logged in `log.md` and the operational log.

#### FR-3.8: Staleness Detection for Saved Query Pages

After updating or deleting file summaries (Phase 2), the `ingest` command SHALL detect and flag query pages whose source material has changed. The change set used here is the full Phase 1 change-set (including deleted summary paths), ensuring that query pages referencing deleted summaries are correctly flagged.

1. Use the **Phase 1 change-set**: the set of summary files (vault paths) that were added, modified, or deleted in Phase 1.
2. For each file in `queries/`, parse its `## Sources` section to extract the list of summary files it references. If a query page has no `## Sources` section, or the `## Sources` section is malformed (e.g., empty, contains no recognizable file paths), this is a hard error: log the issue to `log.md` and the operational debug log, and report the affected file to the user at the end of the run. Do not silently skip such pages.
3. If any referenced summary file appears in the change-set, flag that query page as stale by:
   a. Inserting an Obsidian callout banner immediately after the `# H1` title line (and any blank line that follows it) in the query page file:
      ```
      > [!warning] Stale Content
      > The following source files changed since this answer was saved: `path/to/changed.py.md`
      > Run `codewiki lint` to regenerate this answer.
      ```
      To detect whether a stale banner already exists, the application SHALL scan the file for a line matching the pattern `> [!warning] Stale Content` anywhere in the file. If such a line is found, the page is already flagged stale; ingest SHALL take no additional action for that page (do not add a second banner).
   b. Annotating the query page's row in `index.md`: append ` ⚠ stale` to the Description column value for that row.
4. Append a log entry to `log.md` for each flagged page:
   `YYYY-MM-DD HH:MM:SS UTC | query-stale | queries/filename.md (sources changed: path/to/file.py.md)`
5. After processing: if any pages were flagged, print a terminal summary:
   ```
   ⚠ 2 query pages flagged as stale: queries/how-does-auth-work.md, queries/auth-patterns.md
     Run codewiki lint to regenerate.
   ```
6. On first ingest (no query pages exist yet): this step is a no-op.

### FR-4: Analysis Command

- If `index.md` does not exist when `analysis` is run, the application SHALL print a clear error message (e.g., `"The vault has no summaries. Run 'codewiki ingest' first."`) and exit with code 1.
- At the START of the `analysis` command, before doing any other work, the application SHALL read `index.md` and scan for rows containing ` ⚠ stale` in the Description column. If any stale rows are found, the application SHALL print a warning to the terminal in the following format, then proceed with analysis normally — this is informational only and SHALL NOT block or exit:
  `⚠ 2 query pages are stale: queries/how-does-auth-work.md, queries/auth-patterns.md — run codewiki lint to update.`
- The `analysis` command SHALL read all current summary files in the vault and send them to the LLM. When the combined size of all summaries exceeds the context window limit, the application SHALL use the following batch strategy:
  - The maximum context window for all LLM calls in the `analysis` command is **64,000 tokens** (hardcoded constant: `ANALYSIS_CONTEXT_WINDOW = 64_000`).
  - Use `tiktoken` to estimate the token count of each summary file BEFORE assembling any LLM request, so that batches are guaranteed to fit within the 64,000-token context window limit.
  - Batch summaries by directory tree: start with all summaries under root-level directories as one batch each. If a single root-level directory's summaries still exceed the 64,000-token context window limit, subdivide that directory into its immediate subdirectories and batch those instead. Continue subdividing until each batch fits.
  - Send each batch to the LLM with the analysis prompt to produce a partial overview.
  - Each partial overview SHALL be saved as `overview.md` inside the corresponding vault subdirectory (e.g., summaries batched from `src/auth/` produce `src/auth/overview.md`). If the file already exists it SHALL be overwritten.
  - Each subdirectory `overview.md` SHALL be added to `index.md` using the same two-column table format as other entries, with a description indicating it is a directory overview (e.g., "Directory overview: src/auth/").
  - After all batches are processed, send the collection of partial overviews to the LLM in a final synthesis step to produce the unified root `overview.md`.
- The LLM prompt SHALL instruct the model to produce an overview covering: the apparent purpose of the target application, dominant software engineering patterns observed, consistency or inconsistency in the codebase, and any notable observations or potential issues.
- The root `overview.md` SHALL be written to the vault root. If it already exists it SHALL be overwritten. It SHALL also be listed in `index.md` with a description indicating it is the top-level application overview.
- The `analysis` command SHALL append a timestamped entry to `log.md` recording when analysis was run and the number of summaries reviewed.

### FR-5: Query Command

- If `index.md` does not exist when `query` is run, the application SHALL print a clear error message (e.g., `"The vault has no summaries. Run 'codewiki ingest' first."`) and exit with code 1.
- At the START of the `query` command, before doing any other work, the application SHALL read `index.md` and scan for rows containing ` ⚠ stale` in the Description column. If any stale rows are found, the application SHALL print a warning to the terminal in the following format, then proceed with the query normally — this is informational only and SHALL NOT block or exit:
  `⚠ 2 query pages are stale: queries/how-does-auth-work.md, queries/auth-patterns.md — run codewiki lint to update.`
- The `query` command SHALL accept a natural language question as its argument.
- The `query` command uses a token budget of **128,000 tokens** (hardcoded constant: `QUERY_CONTEXT_WINDOW = 128_000`) — separate from and higher than the 64,000-token budget used by the `analysis` command.
- The application SHALL read `index.md` and send it to the LLM with the question, asking the LLM to identify which summary files are relevant. The LLM SHALL be instructed to return its response as a JSON array of relative file paths **sorted by relevance descending** (most relevant first), e.g., `["src/auth/login.py.md", "src/utils/token.py.md"]`. This sort order serves as the re-ranking mechanism. The application SHALL parse this JSON array to determine which summaries to fetch and in which priority order.
- If the LLM returns an empty JSON array (zero relevant summaries identified), the application SHALL print `"No relevant files found for that query."` and exit with code 3.
- The application SHALL then read the identified relevant summary files and send them, together with the question, to the LLM to produce an answer. When selecting summaries to include, the application SHALL use `tiktoken` to fill up to the 128,000-token budget, filling from the top of the relevance-sorted list (highest relevance) downward:
  - If a single summary file exceeds the 128,000-token budget by itself: skip it, log a warning to the debug log, and list it in the `## Sources` section as `(too large to include)`.
  - If the budget fills before all relevant summaries fit: include as many as fit (highest-relevance first), and append a note to the answer: `"X additional relevant files were found but omitted due to context limits."` (where X is the count of omitted files).
- The answer SHALL be printed to the terminal.
- The answer SHALL end with a trailing `## Sources` section containing a markdown list of the summary files consulted (e.g., `- src/auth/login.py.md`). Files that were identified as relevant but skipped due to size SHALL appear in this list annotated as `(too large to include)`.
- After printing the answer, the CLI SHALL prompt the user: `Save this answer to the wiki? [y/N]`
  - If the user answers `y` or `Y`:
    a. The original question SHALL be slugified to form a filename: convert to lowercase, replace spaces with hyphens, and strip all characters that are not alphanumeric or hyphens. Example: `"How does auth work?"` → `how-does-auth-work.md`.
    b. The answer SHALL be saved as a markdown page in a `queries/` subdirectory at the vault root (e.g., `queries/how-does-auth-work.md`). If a file with the same slug already exists, a numeric suffix SHALL be appended to make the filename unique (e.g., `how-does-auth-work-2.md`, `how-does-auth-work-3.md`, and so on). The application SHALL NOT overwrite an existing query page.
    c. The saved page SHALL contain: the original question as an `# H1` title, the full answer body, the `## Sources` section, and a footer section `## Page Metadata` containing two timestamp fields:
       - `saved_at: YYYY-MM-DD HH:MM:SS UTC` — set once at creation and never changed.
       - `updated_at: YYYY-MM-DD HH:MM:SS UTC` — set at creation; updated whenever the page content changes (answer regenerated by lint, flag added or removed).
    d. An entry for the new page SHALL be added to `index.md` using the same two-column table format as other entries: the **File** column SHALL contain an Obsidian wikilink to the saved page (e.g., `[[queries/how-does-auth-work]]`), and the **Description** column SHALL contain the one-line summary generated by the LLM (see below).
    e. The answer generation LLM prompt SHALL instruct the LLM to return both the answer text and a one-line summary (suitable for the `index.md` Description column) as a structured response. The one-line summary SHALL describe what the answer covers in plain language (e.g., "Explains how the authentication middleware validates JWT tokens"). The application SHALL parse and use the LLM-provided one-line summary for the `index.md` entry; it SHALL NOT use the first sentence of the answer or a fixed template.
    f. A log entry SHALL be appended to `log.md` in the format: `YYYY-MM-DD HH:MM:SS UTC | query-saved | [question] → queries/[filename]`.
  - If the user answers `n`, `N`, or presses Enter without input (default No): the answer is discarded and the application exits cleanly with code 0.
- Saved query pages SHALL be treated as regular wiki pages by future `query` and `analysis` runs — they are listed in `index.md` and will be included in context alongside summary files.
- The `query` command SHALL append a timestamped entry to `log.md` recording the question asked and a brief summary of the response.

### FR-6: Logging

#### FR-6.1: Human-Readable Log (log.md)

- `log.md` SHALL be maintained in the vault root.
- Every `ingest`, `analysis`, and `query` operation SHALL append one or more entries to `log.md`.
- Each entry SHALL begin with a timestamp in the format `YYYY-MM-DD HH:MM:SS UTC` (UTC timezone always).
- Ingest entries SHALL record: operation type, number of files scanned, summarized, skipped (unchanged), skipped (too large), skipped (binary), failed, and deleted.
- Analysis entries SHALL record: operation type and number of summaries reviewed.
- Query entries SHALL record: operation type, the question posed, and the summaries consulted.
- Additional entry types and their formats:
  - `query-saved` — emitted by `query` when the user saves an answer: `YYYY-MM-DD HH:MM:SS UTC | query-saved | [question] → queries/[filename]`
  - `query-stale` — emitted by `ingest` when a query page is flagged as stale: `YYYY-MM-DD HH:MM:SS UTC | query-stale | queries/filename.md (sources changed: path/to/file.py.md)`
  - `lint-resolved` — emitted by `lint` when a stale page is successfully regenerated: `YYYY-MM-DD HH:MM:SS UTC | lint-resolved | queries/filename.md`
  - `lint-unknowable` — emitted by `lint` when a query re-run returns zero relevant files and the page is flagged unknowable: `YYYY-MM-DD HH:MM:SS UTC | lint-unknowable | queries/filename.md`
  - `lint-deduplicated` — emitted by `lint` when a duplicate query page is merged and removed: `YYYY-MM-DD HH:MM:SS UTC | lint-deduplicated | queries/old-page.md → queries/merged-page.md`
  - `lint-query` — emitted by `lint` when a lint-triggered internal query re-run is performed: `YYYY-MM-DD HH:MM:SS UTC | lint-query | queries/filename.md (re-run for staleness resolution)`
- `log.md` is append-only and SHALL never be truncated or overwritten by the application.

#### FR-6.2: Operational Debug Log

- For each run, the application SHALL write a debug log to `logs/<YYYY-MM-DD>_<HH-MM-SS>.log`.
- This log SHALL capture all operational events at DEBUG level or above, including file-by-file processing status, API call outcomes, retry attempts, and any warnings or errors.
- Old log files are NOT automatically rotated or deleted by the application; this is left to the user.

### FR-8: Lint Command

The `lint` command resolves staleness and performs a periodic deep health-check of the wiki.

- If `index.md` does not exist when `lint` is run, the application SHALL print a clear error message and exit with code 1 (same behavior as `analysis` and `query` on an empty vault).
- `lint` exits with code 0 on success (even if stale pages were found and resolved), and code 1 on error.

#### FR-8.1: Part 1 — Staleness Resolution (always runs)

1. Read `index.md` and collect all rows marked ` ⚠ stale` in the Description column.
2. If none are found: print `"No stale query pages found."` and proceed directly to Part 2.
3. For each stale query page (in order):
   a. Before any re-run, remove ALL stale banners from the file. To find stale banners, the application SHALL scan the entire file for any contiguous block that begins with a line matching `> [!warning] Stale Content` and extends through all immediately following lines beginning with `>`. Remove every such block found. This step handles duplicate-banner edge cases and ensures the file is clean before regeneration.
   b. Read the page's H1 heading — this is the original question.
   c. Re-run the full `query` workflow for that question (relevance identification → context filling → answer generation). The save-prompt step is skipped; the answer is saved automatically. This internal re-run SHALL be logged to `log.md` as: `YYYY-MM-DD HH:MM:SS UTC | lint-query | queries/filename.md (re-run for staleness resolution)`. The standard per-query log entry (from FR-5) SHALL NOT be additionally written for lint-triggered re-runs.
   d. If the query re-run returns zero relevant files (the condition that would normally cause `query` to exit with code 3):
      - Replace the answer text with: `"this question cannot be answered by the wiki or the codebase"`
      - Add an unknowable banner immediately after the `# H1` title line in the page file:
        ```
        > [!error] Unknowable
        > This question cannot be answered by the current wiki or codebase.
        > Run `codewiki ingest` then `codewiki lint` if the codebase has changed.
        ```
      - Update `index.md`: replace any ` ⚠ stale` annotation with ` ⊘ unknowable` for that row.
      - Append to `log.md`: `YYYY-MM-DD HH:MM:SS UTC | lint-unknowable | queries/filename.md`
      - Print to terminal: `⊘ Unknowable: queries/how-does-auth-work.md`
      - Update `updated_at` in the page's `## Page Metadata` footer to the current timestamp.
      - Proceed to the next stale page; do NOT abort lint.
   e. Overwrite the existing query page file with the fresh answer (same filename, same slug). The rewritten file starts clean — no stale callout banner.
   f. Update `updated_at` in the page's `## Page Metadata` footer to the current timestamp. The `saved_at` field SHALL NOT be changed.
   g. Update `index.md`: remove the ` ⚠ stale` annotation from that row; update the Description with the fresh LLM-generated one-line summary.
   h. Append to `log.md`: `YYYY-MM-DD HH:MM:SS UTC | lint-resolved | queries/filename.md`
   i. Print to terminal: `✓ Regenerated: queries/how-does-auth-work.md`
4. After all stale pages are resolved: print `"Staleness resolved: N pages updated."` (where N counts only pages that were successfully regenerated, not pages marked unknowable).

#### FR-8.2: Part 2 — Semantic Deduplication and Curation of Query Pages (always runs after Part 1)

The MCP `wiki_query` tool always saves every answer automatically (FR-9.2). This means AI coding agents can accumulate a large number of query pages over time — including near-duplicates, trivially-worded variants of the same question, and low-value exploratory queries. Part 2 of `lint` is the designated mechanism for pruning and curating this accumulation.

The application SHALL detect and merge semantically duplicate saved query pages using a **conservative threshold**: only pages whose questions are nearly identical in meaning (same topic, same intent, trivially different wording) SHALL be merged. Pages that cover the same broad topic from meaningfully different angles SHALL be left intact.

1. Collect all query page rows from `index.md` (rows whose File path is under `queries/`).
2. If fewer than 2 query pages exist: skip deduplication and proceed to Part 3.
3. Send the list of query page filenames and their one-line descriptions (from `index.md`) to the LLM. Instruct the LLM to identify groups of pages that are **near-identical in intent** — i.e., a reasonable reader would consider them duplicates answering the same question. The LLM SHALL apply a conservative threshold and err on the side of keeping pages separate if there is any meaningful difference in scope or angle. The LLM response SHALL be a JSON array of duplicate groups, e.g.: `[["queries/how-does-auth-work.md", "queries/explain-authentication.md"], ...]`. An empty array means no duplicates detected.
4. For each duplicate group identified:
   a. Read the full content of all pages in the group.
   b. Send all pages' content to the LLM and ask it to produce a single merged answer that synthesises the best of all pages in the group. The merged page SHALL use the question from the most recently updated page as its H1 title. The most recently updated page is determined by comparing the `updated_at` timestamp in each page's `## Page Metadata` footer; the page with the latest `updated_at` value is considered the most recent. If `updated_at` is absent or unparseable for any page in the group, fall back to `saved_at`; if both are absent, treat row order in `index.md` as the tiebreaker (later row = more recent).
   c. Determine the output slug: use the filename of the most recently updated page in the group (using the same recency determination as step 4b).
   d. Write the merged page to `queries/<slug>.md`, overwriting the most recent page. Set `saved_at` to the value from the original surviving page; set `updated_at` to the current timestamp.
   e. Delete all other pages in the group from the vault.
   f. Update `index.md`: replace all rows for the group's pages with a single row for the merged page.
   g. Append one `log.md` entry per deleted page: `YYYY-MM-DD HH:MM:SS UTC | lint-deduplicated | queries/old-page.md → queries/merged-page.md`
   h. Print to terminal: `✓ Merged: queries/explain-authentication.md → queries/how-does-auth-work.md`
5. After processing all groups: print `"Deduplication complete: X pages merged into Y pages."` (or `"No duplicate query pages found."` if the array was empty).

**Constraint**: The deduplication LLM call uses only the page titles and descriptions from `index.md` for detection (step 3), not full page content. Full content is only read in step 4a, after a duplicate group is confirmed. This keeps the detection pass cheap.

#### FR-8.3: Part 3 — Deep Health-Check (always runs after Part 2)

The health-check SHALL use the same tiktoken batch approach as the `analysis` command (using the `ANALYSIS_CONTEXT_WINDOW = 64,000` token limit). Batch summary file contents by directory (same subdivision strategy as FR-4), send actual summary file content (not just `index.md`) to the LLM, and synthesize results across batches. `index.md` SHALL also be included in every batch to provide structural context.

For each batch, ask the LLM to identify:
- **Orphan pages**: query pages or summary pages with no inbound backlinks from any other wiki page.
- **Missing cross-references**: pairs of pages that are clearly related but do not link to each other.
- **Contradictions**: claims in one page that appear to contradict claims in another page (best-effort, using actual page content).
- **Concept gaps**: important concepts mentioned across multiple pages but with no dedicated summary page.

After all batches are processed, send the per-batch findings to the LLM in a final synthesis step to produce the unified report.

Write findings to `lint-report.md` in the vault root (overwrite on each run). Format:

```markdown
# Wiki Lint Report
Generated: YYYY-MM-DD HH:MM:SS UTC

## Orphan Pages
- [list or "None found"]

## Missing Cross-References
- [list or "None found"]

## Contradictions
- [list or "None found"]

## Concept Gaps
- [list or "None found"]

## Deduplicated Query Pages
- [list of merges performed this run, or "None"]
```

Print to terminal: `"Lint report written to lint-report.md"`

---

### FR-9: MCP Server Mode

The application SHALL support an MCP (Model Context Protocol) server mode that exposes the `query` command as a structured tool for use by AI coding agents (specifically Claude Code). The MCP server is a **read-oriented interface** — wiki maintenance commands (`ingest`, `analysis`, `lint`) are intentionally excluded and SHALL remain CLI-only, operated by the human user. Exposing maintenance commands to an AI agent risks unintended side effects (e.g., an agent calling `ingest` repeatedly in an attempt to improve query results, producing an expensive loop).

#### FR-9.1: Entry Point

- The MCP server SHALL be launched via a separate `wiki-mcp` entry point defined in `[project.scripts]` in `pyproject.toml`.
- The MCP server SHALL run from the vault root directory (same constraint as the CLI: current working directory at startup is the vault root).

#### FR-9.2: Exposed MCP Tools

The MCP server SHALL expose exactly one tool:

**`wiki_query`**
- Input: `{"question": str}`
- Output: `{"answer": str, "sources": [list of relative file paths], "saved_path": str, "stale_warning": list[str]|null}`
- `stale_warning` is an array of vault-relative file paths of query pages currently flagged as stale (e.g., `["queries/how-does-auth-work.md", "queries/auth-patterns.md"]`), or `null` if no stale pages exist. Returning an array (rather than a human-readable string) allows agent callers to programmatically inspect which pages are stale.
- There is no interactive prompt in MCP mode. The answer SHALL always be saved automatically to `queries/` (same slug logic as CLI mode), `index.md` SHALL be updated, and `log.md` SHALL be appended on every `wiki_query` call. `saved_path` in the response SHALL always contain the relative path of the saved file (e.g., `"queries/how-does-auth-work.md"`). There is no `save` parameter; saving is unconditional.
- Rationale: AI coding agents call `wiki_query` programmatically and cannot respond to interactive prompts. Always saving ensures the question-answer pair is captured for future reference, deduplication, and staleness tracking. Noise from accumulated agent queries is managed by the `lint` command's deduplication step (FR-8.2).
- If stale query pages exist, their vault-relative file paths SHALL be included in the `stale_warning` array rather than printed to terminal.

#### FR-9.3: Behavior Differences from CLI Mode

- No interactive prompts (MCP callers do not interact via stdin). The answer is always saved automatically; there is no save prompt and no `save` parameter.
- No rich terminal output (no progress bars, no ANSI color codes).
- All output is structured JSON returned via MCP protocol.
- Errors are returned as MCP error responses, not printed to stderr.
- Because every MCP `wiki_query` call writes a query page, repeated or exploratory agent queries may accumulate many entries in `queries/`. The `lint` command's deduplication step (FR-8.2) is the designated mechanism for pruning this noise.

#### FR-9.4: Protocol

- The MCP server SHALL implement the MCP stdio transport (stdin/stdout JSON-RPC 2.0), as specified by the Model Context Protocol.
- The MCP server SHALL share the same underlying business logic as the CLI `query` command. Only the I/O layer differs.

---

### FR-10: Help Command

The `help` subcommand provides usage documentation for the CLI and MCP setup instructions.

#### FR-10.1: Command Overview (`codewiki help`)

`codewiki help` (with no arguments) SHALL print an overview table of all commands and exit with code 0:

```
Codebase Wiki Builder — commands:
  ingest    Scan target codebase and update wiki summaries
  analysis  Analyze summaries and write overview.md
  query     Ask a question answered from the wiki
  lint      Resolve stale query pages and health-check the wiki
  help      Show help for commands and MCP setup
```

#### FR-10.2: Per-Command Help (`codewiki help <command>`)

`codewiki help <command>` — where `<command>` is one of `ingest`, `analysis`, `query`, `lint`, or `mcp` — SHALL print a detailed help page for that command. Each page SHALL include:

- **Purpose**: what the command does
- **What it reads**: input files/config it consumes
- **What it writes**: files it creates or modifies
- **Exit codes**: which exit codes it may return and under what conditions
- **Notable behaviors**: any caveats (e.g., "`query` will warn about stale pages before answering; the save-to-wiki prompt can be declined")

`codewiki help <command>` SHALL exit with code 0.

If `<command>` is not one of the recognized values (`ingest`, `analysis`, `query`, `lint`, `mcp`), the application SHALL:
1. Print an error message identifying the unrecognized argument (e.g., `Error: unrecognized help topic "foo"`).
2. Print the general help text (same output as `codewiki help` with no arguments).
3. Exit with code 0.

#### FR-10.3: MCP Setup Instructions (`codewiki help mcp`)

`codewiki help mcp` SHALL print human-readable MCP setup instructions and exit with code 0. The output SHALL:

1. Resolve the current working directory (vault root) as an absolute path at runtime.
2. Print a human-readable explanation of what the MCP server does and why.
3. Print the exact `.mcp.json` snippet the user should add to their target codebase's root, with the vault's absolute path already substituted in:

   ```
   To connect Claude Code to this wiki, add the following to
   .mcp.json in the root of your target codebase:

   {
     "mcpServers": {
       "wiki": {
         "command": "uv",
         "args": ["run", "--project", "/absolute/path/to/vault", "wiki-mcp"]
       }
     }
   }

   Then restart Claude Code in the target codebase. Claude Code will
   automatically discover the wiki_query tool.
   ```

   The `/absolute/path/to/vault` SHALL be the resolved absolute path of the current directory at the time `codewiki help mcp` is invoked.

**Exit codes**: `codewiki help` (all forms) always exits 0.

---

### FR-7: Obsidian Plugin Management (Optional / Exploratory)

> **Note**: The Obsidian CLI was released in v1.12.4 (February 2026). Documentation is limited at the time of writing. Implementations SHALL treat this entire feature as best-effort and exploratory. No acceptance test depends on it.

- The application MAY attempt to enable the Obsidian Search core plugin on startup by invoking the Obsidian CLI against the active vault (the directory the tool is run from). No explicit vault name configuration is required; the CLI targets the currently active vault.
- This operation is entirely optional and MUST NOT block or fail the application if Obsidian is not installed, not running, or does not respond within 5 seconds.
- Direct filesystem operations (writing markdown files) are the primary vault interaction mechanism and SHALL function without the Obsidian CLI.
- The Obsidian CLI is used exclusively for plugin management (enable/disable). No vault file creation, modification, or deletion SHALL go through the Obsidian CLI.
- Obsidian v1.12.4 or later is required for CLI support. The application SHALL note this requirement in its documentation.

---

## Non-Functional Requirements

### Reliability

- The application SHALL survive transient LLM API failures without losing vault state or crashing. Each file is an independent operation.
- If the process is interrupted mid-ingest, any summaries already written are valid; the next run will correctly re-evaluate remaining files via MD5 checking.
- The application SHALL validate that the target codebase path is a readable directory before beginning ingest.

### Security

- This is a local, single-user application. Secrets (API keys) are stored in `.env` only.
- `.env` and `.wiki-config.json` SHALL be listed in `.gitignore` to prevent accidental commit of secrets.
- No network communication occurs except to the configured LLM API endpoint.
- The application does not implement access controls, authentication, or encryption (out of scope for a local tool).

### Exit Codes

The application SHALL exit with the following codes:

| Code | Meaning | Conditions |
|------|---------|------------|
| `0` | Success | Command completed successfully |
| `1` | General error | Config missing/invalid, API failure, codebase path not found, empty vault on `analysis`, `query`, or `lint` |
| `2` | Partial success | `ingest` completed but one or more files failed to summarize |
| `3` | No results | `query` found no relevant summaries in `index.md` |

---

## Explicit Constraints (DO NOT)

- Do NOT use the OpenAI SDK as the primary LLM client. Use the native `anthropic` Python SDK. OpenAI may be supported as an optional alternative backend, but the default and primary integration is Anthropic.
- Do NOT use the Obsidian CLI for creating, modifying, or deleting vault files. All vault file operations SHALL use direct filesystem writes.
- Do NOT use Click or argparse as the CLI framework. Use Typer.
- Do NOT hard-code API keys, codebase paths, or model names anywhere in source code.
- Do NOT write secrets to `.wiki-config.json`; secrets go in `.env` only.
- Do NOT truncate or overwrite `log.md`; it is append-only.
- Do NOT implement a web server, REST API, or any HTTP-based networked interface. The only external communication channels are: the configured LLM API (outbound HTTPS) and the MCP stdio transport (stdin/stdout, local IPC only).
- Do NOT duplicate business logic between the CLI and MCP server. The MCP server is a thin transport wrapper; the CLI and MCP server share the same underlying core functions.
- Do NOT implement push notifications, real-time watching, or filesystem watchers (deferred to future phases).
- Do NOT implement tree-sitter AST-based chunking in the initial version; the file-size threshold guard is sufficient for MVP.
- Do NOT use the OpenAI Batch API in the initial version; sequential processing with delay/backoff is sufficient for MVP.
- Do NOT block on Obsidian CLI availability; graceful degradation is required.
- Do NOT support remote (SSH, cloud) target codebases; only local filesystem paths are supported.
- Do NOT overwrite an existing saved query page: if `queries/<slug>.md` already exists, append a numeric suffix (e.g., `<slug>-2.md`) until a unique filename is found. This applies to both CLI and MCP `wiki_query` saves.
- Do NOT expose a `save` parameter on the MCP `wiki_query` tool. MCP saving is unconditional; the parameter does not exist and SHALL NOT be accepted.

---

## Technical Context

### Existing Codebase State

This is a greenfield project. No source files, package manifests, or application code exist yet. The repository contains only:
- MAPS workflow tooling (`.maps/`, `.claude/`, `.mcp.json`)
- A stub `README.md`
- A `.gitignore` pre-configured for the Python ecosystem (ruff, mypy, pytest, uv, .venv, .env)

### Target Stack

- **Language**: Python 3.10+
- **Package manager**: `uv` with `pyproject.toml`
- **CLI framework**: Typer (wraps Click; type-hint-based subcommands)
- **Primary LLM SDK**: `anthropic` (native Python SDK)
- **Secondary LLM SDK**: `openai` (optional alternative backend)
- **Secrets loading**: `python-dotenv`
- **Hashing**: `hashlib` (stdlib)
- **Token counting**: `tiktoken` (for context window management in analysis batching and query retrieval)
- **Context window constants** (hardcoded, not configurable):
  - `ANALYSIS_CONTEXT_WINDOW = 64_000` — maximum tokens per LLM call in the `analysis` command
  - `QUERY_CONTEXT_WINDOW = 128_000` — token budget for the summary-retrieval LLM call in the `query` command
- **Filesystem**: `pathlib` (stdlib)
- **Terminal output**: `rich` (bundled with Typer)
- **Linter**: Ruff
- **Type checker**: mypy
- **Test runner**: pytest

### pyproject.toml Structure

The `pyproject.toml` SHALL include:
- `[project]` with name, version, Python version constraint, and dependencies
- `[project.scripts]` defining two entry points:
  - `codewiki` — the CLI entry point (e.g., `codewiki = "codebase_wiki_builder.cli:app"`)
  - `wiki-mcp` — the MCP server entry point (e.g., `wiki-mcp = "codebase_wiki_builder.mcp_server:main"`)
- `[tool.ruff]` configuration
- `[tool.mypy]` configuration
- `[tool.pytest.ini_options]` configuration

### LLM Provider Abstraction

The application SHALL implement a thin LLM client abstraction (a module or class) with a primary method that accepts file content and returns a markdown summary string. This abstraction SHALL route to either the `anthropic` or `openai` SDK based on the provider setting in `.wiki-config.json`. The default provider is `anthropic` with model `claude-sonnet-4-6`; the default OpenAI model (if selected) is `gpt-4o`.

### Vault File Layout

```
<vault-root>/
  .wiki-config.json       # non-secret config (gitignored)
  .env                    # secrets: API keys (gitignored)
  index.md                # catalog of all summaries
  log.md                  # human-readable append-only operation log
  overview.md             # analysis output (overwritten each analysis run)
  lint-report.md          # most recent deep health-check report (overwritten each lint run)
  logs/
    YYYY-MM-DD_HH-MM-SS.log   # operational debug log per run
  queries/
    <slugified-question>.md       # saved query answers and lint-regenerated answers; each contains ## Page Metadata footer with saved_at/updated_at
  <mirrored codebase directories>/
    <source-file-name>.<ext>.md   # one summary per source file, named <name>.<ext>.md
```

### Obsidian Backlink Format

Backlinks SHALL use Obsidian wikilink syntax: `[[relative/path/to/file]]`. The path SHALL be relative to the vault root and SHALL omit the `.md` extension, consistent with Obsidian's wikilink convention.

### Obsidian CLI Integration

The Obsidian CLI is available as of Obsidian v1.12.4 (released February 27, 2026). The CLI communicates with a running Obsidian instance via IPC. Commands use bare-word `key=value` parameter syntax (e.g., `obsidian plugin:enable id=search`). Since the CLI requires Obsidian desktop to be running, all invocations SHALL be wrapped in error handling with a timeout, and failures SHALL be logged as warnings rather than errors.

---

## Data Requirements

### Data Model

| Entity | Location | Description |
|--------|----------|-------------|
| Summary file | `<vault>/<mirrored-path>/<filename>.<ext>.md` | LLM-generated markdown summary of one source file |
| Index | `<vault>/index.md` | Catalog of all summaries and saved query pages with links and one-line descriptions |
| Log | `<vault>/log.md` | Append-only human-readable operation history |
| Debug log | `<vault>/logs/<date>_<time>.log` | Per-run operational debug log |
| Overview | `<vault>/overview.md` | LLM-generated analysis of entire codebase |
| Saved query page | `<vault>/queries/<slug>.md` | User-saved query answer or lint-regenerated answer filed back into the wiki; contains `## Page Metadata` footer with `saved_at` and `updated_at` UTC timestamp fields |
| Lint report | `<vault>/lint-report.md` | Most recent deep health-check report; overwritten on each `lint` run |
| Config | `<vault>/.wiki-config.json` | Non-secret runtime configuration |
| Secrets | `<vault>/.env` | LLM API keys |

### Data Flow

1. `ingest` (Phase 1): Scan source files → compute MD5 → compare with stored hashes → record new/modified/deleted entries in change-set (no vault writes). (Phase 2): Write new/updated summaries → delete removed summaries + clean backlinks → update index → scan query pages for staleness using the Phase 1 change-set (including deleted paths) → flag stale pages (banner after H1 + index annotation) → update log.
2. `analysis`: Scan `index.md` for stale rows → print warning if any → read all summary files → send to LLM (tiktoken batched) → write `overview.md` → update log.
3. `query` (CLI): Scan `index.md` for stale rows → print warning if any → read `index.md` → identify relevant summaries via LLM → read those summaries → send question + summaries to LLM (LLM returns answer + one-line summary) → print answer → prompt user to save → if saved: write `queries/<slug>.md` (with `saved_at`/`updated_at` footer), update `index.md` (with LLM-generated one-line description), append `query-saved` entry to `log.md` → append query entry to `log.md`.
   `wiki_query` (MCP): Scan `index.md` for stale rows → include vault-relative paths in `stale_warning` array if any → read `index.md` → identify relevant summaries via LLM → read those summaries → send question + summaries to LLM → always write `queries/<slug>.md`, update `index.md`, append `query-saved` entry to `log.md` → return structured JSON response.
4. `lint`: Read `index.md` → Part 1: for each stale page: strip all stale banners → re-run query workflow (log as `lint-query`) → if zero relevant files: flag page unknowable (banner + `lint-unknowable` log entry) → else: overwrite page with fresh answer + update `updated_at` → remove stale annotation from index → append `lint-resolved` to `log.md` → Part 2: send query page titles/descriptions to LLM → identify near-identical duplicates → for each group: read full content → merge via LLM (recency by `updated_at`) → overwrite surviving page (preserve `saved_at`, update `updated_at`) → delete others → update index → append `lint-deduplicated` to `log.md` → Part 3: batch summary content via tiktoken (same as analysis) → send to LLM for health-check → write `lint-report.md`.

### MD5 Storage Format

The MD5 hash SHALL be stored in each summary file as an HTML comment on its own line at the end of the file:
```
<!-- md5: <32-character hexdigest> -->
```
This format is human-readable, survives Obsidian rendering, and is easily parsed with a simple regex.

---

## User Workflows

**Scenario: First ingest of a new codebase**
- **Given**: The user has navigated to an empty or new Obsidian vault directory
- **When**: The user runs `codewiki ingest`
- **Then**: The application detects no `.wiki-config.json`, prompts for the target codebase path, validates it, writes `.wiki-config.json`, then scans and summarizes all eligible files, writes all summary files and `index.md`, and appends an entry to `log.md`

**Scenario: Incremental ingest after code changes**
- **Given**: The vault has existing summaries and `.wiki-config.json`
- **When**: Some source files have been modified and the user runs `codewiki ingest`
- **Then**: Only the modified files (different MD5) are re-summarized; unchanged files are skipped; deleted files' summaries are removed and backlinks cleaned up; `index.md` is regenerated

**Scenario: Source file deleted**
- **Given**: A source file `src/utils/helper.py` was previously summarized
- **When**: The file is deleted from the target codebase and the user runs `codewiki ingest`
- **Then**: `src/utils/helper.py.md` is deleted from the vault; every other summary containing `[[src/utils/helper.py]]` in its References section has that backlink removed; the deletion is recorded in `log.md`

**Scenario: Query the wiki**
- **Given**: The vault has summaries and an `index.md`
- **When**: The user runs `codewiki query "How does the application handle authentication?"`
- **Then**: The LLM identifies relevant summaries from `index.md`, reads them, and returns a grounded answer with citations

**Scenario: Save a query answer to the wiki**
- **Given**: The vault has summaries and an `index.md`
- **When**: The user runs `codewiki query "How does the application handle authentication?"` and answers `y` at the save prompt
- **Then**: The answer is written to `queries/how-does-the-application-handle-authentication.md` with the question as the H1 title, the full answer body, and the `## Sources` section; `index.md` gains a new row for the saved page; `log.md` gains a `query-saved` entry

**Scenario: Ingest flags stale query pages**
- **Given**: The vault has a saved query page `queries/how-does-auth-work.md` whose `## Sources` reference `src/auth/login.py.md`
- **When**: `src/auth/login.py` is modified and `codewiki ingest` is run
- **Then**: `queries/how-does-auth-work.md` gains a `> [!warning] Stale Content` banner immediately after the `# H1` title line; `index.md` shows ` ⚠ stale` on that row; `log.md` gains a `query-stale` entry; the terminal prints a stale-pages summary with a reminder to run `codewiki lint`

**Scenario: Lint resolves stale pages and runs health-check**
- **Given**: The vault has one or more stale query pages (with stale banners and ` ⚠ stale` in `index.md`)
- **When**: The user runs `codewiki lint`
- **Then**: Each stale page is regenerated (fresh answer, no stale banner), `index.md` annotations are cleaned up, `log.md` gains `lint-resolved` entries, and `lint-report.md` is written with the four health-check sections

**Scenario: Obsidian not installed**
- **Given**: The user's machine does not have Obsidian installed
- **When**: The user runs any wiki command
- **Then**: The application logs a warning that Obsidian CLI is unavailable, skips the plugin-enable step, and proceeds normally; all vault files are written directly to the filesystem

---

## Acceptance Tests

1. **Fresh ingest**: Run `codewiki ingest` in an empty vault against a small test codebase (10 files). Verify: 10 summary files exist, `index.md` lists all 10, `log.md` has one entry, a debug log exists under `logs/`.

2. **Incremental ingest**: Modify 2 of the 10 test files. Run `codewiki ingest` again. Verify: for each of the 8 unchanged source files, the MD5 hash embedded in the summary footer (`<!-- md5: ... -->`) still matches the current MD5 of the corresponding source file, confirming those summaries were not re-summarized. For the 2 modified files, the footer hash SHALL match the new source file MD5.

3. **Binary file exclusion**: Include a PNG and a compiled `.pyc` file in the test codebase. Verify: no summary files are created for them; they are logged as skipped.

4. **Oversized file exclusion**: Include a file larger than the configured threshold. Verify: no summary is created; the file appears in `log.md` as skipped-too-large.

5. **Deleted file cleanup**: Delete one source file from the test codebase. Run `codewiki ingest`. Verify: its summary is gone from the vault; any other summaries that referenced it no longer contain that backlink.

6. **Analysis command**: Run `codewiki analysis`. Verify: `overview.md` exists and is non-empty; `log.md` has an analysis entry.

7. **Query command**: Run `codewiki query "What is this codebase?"`. Verify: a non-empty answer is printed; the answer ends with a `## Sources` section containing a markdown list of the summary files consulted; `log.md` has a query entry.

8. **First-run prompt**: Delete `.wiki-config.json`. Run `codewiki ingest`. Verify: the application prompts for the codebase path; after entry, `.wiki-config.json` is created and ingest proceeds.

9. **Rate limit retry**: Mock the LLM API to return HTTP 429 twice then succeed. Verify: the application retried twice before succeeding; the retry attempts are logged.

10. **MD5 hash footer**: Open any generated summary file. Verify: the last line matches the pattern `<!-- md5: [a-f0-9]{32} -->` and the hash matches the MD5 of the corresponding source file.

11. **Query oversized summary**: Mock a summary file whose token count exceeds 128,000 tokens. Run `codewiki query` against a vault containing that file as the sole relevant result. Verify: the answer's `## Sources` section lists the file annotated as `(too large to include)`; a warning is written to the debug log; the application exits with code 0 (not a hard failure).

12. **Query context overflow truncation**: Mock a vault where relevant summaries collectively exceed 128,000 tokens but each individual file is within budget. Run `codewiki query`. Verify: the answer includes only the highest-relevance summaries that fit within 128,000 tokens; the answer body contains the note `"X additional relevant files were found but omitted due to context limits."` with X equal to the count of omitted files; the `## Sources` section lists only the files that were actually included.

13. **Query answer persistence**: Given a vault with ingested summaries, run `codewiki query "What patterns does this codebase use?"` and answer `y` at the save prompt. Verify: (a) a new file exists at `queries/what-patterns-does-this-codebase-use.md`; (b) that file contains the original question as an `# H1` title, the full answer body, a `## Sources` section, and a `## Page Metadata` footer with `saved_at` and `updated_at` timestamp fields; (c) `index.md` contains a new row for `queries/what-patterns-does-this-codebase-use.md` with a one-line description (LLM-generated, not a template); (d) `log.md` contains a `query-saved` entry referencing the question and the saved filename.

14. **Lint staleness resolution**: Given a vault with `queries/how-does-auth-work.md` that has a `> [!warning] Stale Content` banner immediately after the `# H1` title (not at the very top of the file) and a ` ⚠ stale` annotation in its `index.md` row. When `codewiki lint` is run. Verify: (a) `queries/how-does-auth-work.md` is overwritten with fresh content and contains no stale banner; (b) the `# H1` title is still the first line of the file; (c) the `index.md` row for that page has no ` ⚠ stale` annotation; (d) `log.md` contains both a `lint-query` entry and a `lint-resolved` entry for `queries/how-does-auth-work.md` (and no standard query-command entry); (e) the terminal output included `✓ Regenerated: queries/how-does-auth-work.md`; (f) the `updated_at` field in `## Page Metadata` has been updated to the current run timestamp; (g) the `saved_at` field is unchanged from its original value.

15. **Lint unknowable page**: Given a vault with `queries/how-does-feature-x-work.md` that has a ` ⚠ stale` annotation in its `index.md` row, and where re-running the query returns zero relevant files. When `codewiki lint` is run. Verify: (a) the page is overwritten with answer text "this question cannot be answered by the wiki or the codebase"; (b) the page has a `> [!error] Unknowable` banner immediately after the `# H1` title; (c) `index.md` shows ` ⊘ unknowable` (not ` ⚠ stale`) for that row; (d) `log.md` contains a `lint-unknowable` entry for that page; (e) `lint` continued processing and did not abort.

16. **Lint semantic deduplication**: Given a vault containing two saved query pages: `queries/how-does-auth-work.md` (description: "Explains how authentication works") and `queries/explain-authentication.md` (description: "Describes the authentication system") — near-identical in intent. When `codewiki lint` is run. Verify: (a) one of the two files is deleted; (b) the surviving file contains merged content with an `# H1` title, full answer, `## Sources` section, and a `## Page Metadata` footer where `saved_at` matches the original surviving page and `updated_at` reflects the current run timestamp; (c) `index.md` contains exactly one row for the merged page and no row for the deleted page; (d) `log.md` contains a `lint-deduplicated` entry referencing both filenames; (e) the terminal output included `✓ Merged:` with both filenames.

17. **Lint deep health-check**: Given a vault with ingested summaries and no stale query pages. When `codewiki lint` is run. Verify: `lint-report.md` exists in the vault root and contains all four section headers: `## Orphan Pages`, `## Missing Cross-References`, `## Contradictions`, and `## Concept Gaps`.

18. **`codewiki help mcp` output**: Run `codewiki help mcp` from the vault root directory. Verify: (a) the command exits with code 0; (b) the output contains a JSON block with a `mcpServers` key; (c) the output contains the resolved absolute path of the current vault directory (i.e., `os.getcwd()` at runtime) substituted into the `--project` argument.

19. **MCP `wiki_query` tool call (always saves)**: Start the `wiki-mcp` server against a vault with ingested summaries. Send a valid MCP `tools/call` request for `wiki_query` with input `{"question": "What does this codebase do?"}`. Verify: (a) the response contains a JSON object with `answer` (non-empty string), `sources` (non-empty list), and `saved_path` (a non-null string, e.g., `"queries/what-does-this-codebase-do.md"`); (b) the file exists at that path with an `# H1` title equal to the original question, the full answer body, and a `## Sources` section; (c) `index.md` contains a new row for the saved page; (d) `log.md` contains a `query-saved` entry referencing the question and the saved filename.

20. **MCP `wiki_query` no `save` parameter accepted**: Send a valid MCP `tools/call` request for `wiki_query` with input `{"question": "What does this codebase do?", "save": false}`. Verify: the server returns an MCP error response indicating that `save` is not a recognized parameter (or alternatively ignores it and still saves — the implementation SHALL NOT honor a `save: false` input to suppress saving).

21. **MCP `wiki_query` stale_warning is array**: Given a vault with two stale query pages. Start the `wiki-mcp` server and send a `wiki_query` request. Verify: the `stale_warning` field in the response is a JSON array of vault-relative file path strings (e.g., `["queries/page-a.md", "queries/page-b.md"]`), not a plain string.

22. **Ingest stale banner placement**: Given a vault with a saved query page `queries/how-does-auth-work.md` (H1 title on first line, then answer content). Modify a source file referenced in its `## Sources` section and run `codewiki ingest`. Verify: (a) the stale banner (`> [!warning] Stale Content`) appears immediately after the `# H1` title line, not before it; (b) the `# H1` title is still the first line of the file.

23. **Ingest stale banner no-duplicate**: Given a vault with `queries/how-does-auth-work.md` that already has a stale banner. Run `codewiki ingest` again with no changes to the referenced source files (the page remains stale). Verify: the file contains exactly one stale banner block — not two.

24. **Ingest missing Sources hard error**: Given a vault with `queries/malformed-query.md` that has no `## Sources` section. Run `codewiki ingest`. Verify: (a) the run does not silently skip the page; (b) an error entry for `queries/malformed-query.md` appears in `log.md` and the operational debug log; (c) the affected filename is reported to the user at the end of the run; (d) the run does not exit with code 1 solely due to this condition (ingest continues processing other files).

25. **`codewiki help` unrecognized argument**: Run `codewiki help foo`. Verify: (a) the output contains an error message identifying "foo" as unrecognized; (b) the output also contains the general help table listing all commands; (c) the command exits with code 0.

---

## Dependencies

### Upstream Dependencies
- Python 3.10+ runtime
- `uv` package manager (development)
- Anthropic API access (ANTHROPIC_API_KEY) — required for default configuration
- Obsidian v1.12.4+ desktop (optional — required only for plugin management)

### Downstream Impact
- Vault consumers (human developers, LLM agents) depend on the accuracy and freshness of the generated wiki.

### External Dependencies

| Dependency | Purpose | Required? |
|------------|---------|-----------|
| `anthropic` Python SDK | Primary LLM client | Yes (default provider) |
| `openai` Python SDK | Alternative LLM client | No (optional provider) |
| `typer` | CLI framework | Yes |
| `python-dotenv` | Load `.env` secrets | Yes |
| `rich` | Terminal output | Yes (bundled with Typer) |
| `tiktoken` | Token counting for context window management (analysis batching, query retrieval) | Yes |
| `tenacity` (or stdlib) | Retry logic with backoff | Yes |
| `mcp` | Python MCP SDK — stdio transport for the MCP server (FR-9) | Yes |
| Obsidian CLI (v1.12.4+) | Plugin management only | No (graceful degradation) |

---

## Risks and Mitigations

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| LLM rate limits during bulk ingest | High | Medium | Configurable delay + exponential backoff retry (up to 5 attempts); exits with code 1 on exhaustion |
| Large files exceeding context window | Medium | Low | Configurable size threshold; files above threshold are skipped and logged |
| Obsidian CLI not available | Medium | Low | Graceful degradation; plugin management is optional; core operations use filesystem directly |
| Backlink cleanup O(n) scan performance on large vaults | Low | Low | Acknowledged; acceptable for MVP; an inverted index can be added in v2 if needed |
| MD5 false negative (collision) | Negligible | Low | Acceptable for local dev tool; not a security use case |
| `.env` committed accidentally | Medium | High | `.env` in `.gitignore`; documented in setup instructions |

---

## Out of Scope

- Real-time filesystem watching or automatic re-ingest on file change
- Remote or cloud-hosted target codebases (SSH, S3, etc.)
- A web UI or REST API
- Tree-sitter AST-based chunking for large files (deferred to v2)
- OpenAI Batch API for cost-efficient bulk processing (deferred to v2)
- Multi-vault management within a single command invocation
- User authentication or multi-user access controls
- Encrypted storage of summaries
- Semantic search or embeddings-based retrieval (the `query` command uses direct LLM context, not vector search)
- `obsidian search` command integration for in-Obsidian search (the vault search works natively once files are written)
- MCP authentication (the MCP server is local-only; no auth layer is required)
- Multi-vault MCP server (one `wiki-mcp` instance serves one vault; running multiple vaults requires multiple server instances)
- MCP access to maintenance commands (`ingest`, `analysis`, `lint`) — these are CLI-only, operated by the human user; exposing them to AI agents is explicitly out of scope

---

## Open Questions

All questions from Critical Reviews #1, #2, and #3 have been resolved and incorporated into this specification. No open questions remain.

---

## Appendices

### Glossary

- **Target codebase**: The source code directory being wikified (not this application's own code)
- **Vault**: The Obsidian vault directory where wiki files are written
- **Summary file**: A markdown file in the vault describing one source file from the target codebase
- **Backlink**: An Obsidian wikilink (`[[path/to/file]]`) in one summary file pointing to another
- **MD5 footer**: The `<!-- md5: <hash> -->` comment at the end of each summary file used for change detection
- **Ingest**: The operation of scanning the target codebase and updating the vault
- **Analysis**: The operation of reviewing all summaries and producing `overview.md`
- **Query**: The operation of answering a natural language question using the wiki contents
- **Saved query page**: A markdown file in `queries/` containing a user-saved query answer, treated as a first-class wiki page in `index.md` and accessible to future `query` and `analysis` runs
- **Slug**: A URL-friendly filename derived from a query question: lowercased, spaces replaced with hyphens, non-alphanumeric characters stripped
- **Stale query page**: A saved query page whose referenced source summaries have changed since the answer was saved; flagged with a `> [!warning] Stale Content` banner (placed immediately after the `# H1` title) and ` ⚠ stale` annotation in `index.md`
- **Unknowable page**: A saved query page for which a lint-triggered re-run returned zero relevant files; flagged with a `> [!error] Unknowable` banner (placed immediately after the `# H1` title) and ` ⊘ unknowable` annotation in `index.md`. Distinct from stale: stale means sources changed, unknowable means no sources can answer the question at all
- **Page Metadata footer**: A `## Page Metadata` section at the end of every saved query page containing `saved_at` (set once at creation) and `updated_at` (updated on every content change) UTC timestamp fields. Used by lint deduplication to determine recency
- **Lint**: The operation of resolving stale query pages, flagging unanswerable pages as unknowable, deduplicating near-identical query pages, and performing a deep health-check of the vault; produces `lint-report.md`
- **MCP server**: The `wiki-mcp` entry point that implements the Model Context Protocol stdio transport, exposing `wiki_query` as a structured tool callable by AI coding agents such as Claude Code. Maintenance commands (`ingest`, `analysis`, `lint`) are intentionally excluded from the MCP interface and remain CLI-only
- **MCP tool**: A named, schema-defined callable exposed by the MCP server. The server exposes exactly one tool — `wiki_query` — which mirrors the CLI `query` subcommand but returns structured JSON instead of human-readable terminal output, and always saves the answer automatically (no interactive save prompt, no `save` parameter). Maintenance commands (`ingest`, `analysis`, `lint`) are CLI-only and not exposed via MCP
- **stdio transport**: The MCP communication channel in which the server reads JSON-RPC 2.0 messages from stdin and writes responses to stdout; used for local IPC between Claude Code and the `wiki-mcp` process

### References

- Karpathy Wiki-Builder Gist: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
- Obsidian CLI documentation: https://obsidian.md/cli
- Obsidian CLI release announcement: https://dev.to/shimo4228/obsidians-official-cli-is-here-no-more-hacking-your-vault-from-the-back-door-3123
- Anthropic Python SDK: https://github.com/anthropics/anthropic-sdk-python
- Typer documentation: https://typer.tiangolo.com/
- uv documentation: https://docs.astral.sh/uv/guides/projects/

### Version History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.8.0 | 2026-04-29 | Architect Agent | Incorporated all 12 resolved questions from Critical Review #3: Q-30: stale banner placement moved to after the H1 title (not before it). Q-31: stale banner detection via text-pattern scan (`> [!warning] Stale Content`); ingest skips pages already flagged stale; lint removes ALL stale banners before re-evaluating. Q-32: missing or malformed `## Sources` section is a hard error — logged and reported to user. Q-33: lint query re-run returning zero relevant files flags page as "unknowable" (new flag, distinct from stale, same placement/detection rules); answer replaced with canonical text; `lint-unknowable` log entry; lint continues. Q-34: `saved_at` and `updated_at` timestamp fields added to `## Page Metadata` footer of every saved query page; `saved_at` set once at creation; `updated_at` updated on every content change; lint deduplication uses `updated_at` for recency determination. Q-35: FR-8.3 health-check now uses same tiktoken batch approach as analysis (ANALYSIS_CONTEXT_WINDOW = 64,000 tokens); sends actual summary file content to LLM (not just index.md); final synthesis step aggregates per-batch findings. Q-36: `stale_warning` in FR-9.2 MCP response schema changed from `str|null` to `list[str]|null` (array of vault-relative file paths). Q-37: `codewiki help <unrecognized>` prints error identifying the argument, then general help text, then exits 0. Q-38: acceptance tests renumbered sequentially in document order (1–25); 7 new tests added for new behaviors. Q-39: ingest now uses mandatory two-phase approach — Phase 1 computes full change-set (new/modified/deleted) without making vault changes; Phase 2 applies all changes using Phase 1 change-set; deleted summary paths are in change-set before deletions occur. Q-40: lint internal query re-runs logged as `lint-query` entries in log.md; standard per-query log entry suppressed during lint-triggered re-runs. Q-41: LLM answer generation prompt extended to return both the answer and a one-line summary for index.md; application parses and uses LLM-provided one-line summary. |
| 1.7.0 | 2026-04-29 | Architect Agent | Pure rename: CLI command prefix changed from `wiki` to `codewiki` throughout (e.g. `wiki ingest` → `codewiki ingest`, `wiki query` → `codewiki query`, `wiki analysis` → `codewiki analysis`, `wiki lint` → `codewiki lint`, `wiki help` → `codewiki help`). Updated FR-1 entry point name, pyproject.toml script name, FR-10 section headings, all user workflow scenarios, all acceptance tests, all error/warning message literals. MCP tool names (`wiki_query`, `wiki_ingest`, etc.), `wiki-mcp` entry point name, and MCP server JSON key name are unchanged. No behavioral changes. |
| 1.6.0 | 2026-04-29 | Architect Agent | Revised FR-9.2: removed `save` parameter from `wiki_query` MCP tool; MCP `wiki_query` now always saves the answer automatically to `queries/` (unconditional auto-save). Updated FR-9.3 behavior differences accordingly. Added rationale: agents cannot respond to interactive prompts, and lint deduplication manages accumulation noise. Strengthened FR-8.2 preamble to explicitly identify it as the designated pruning/curation mechanism for agent-accumulated query pages. Updated acceptance tests #17 and #18 to match new MCP behavior. Updated data flow for MCP `wiki_query`. Updated Explicit Constraints to add no-`save`-parameter rule and clarify numeric-suffix deduplication applies to both CLI and MCP. Updated "MCP tool" glossary entry. |
| 1.5.0 | 2026-04-27 | Architect Agent | Added FR-9 (MCP server mode): `wiki-mcp` entry point, four MCP tools (`wiki_ingest`, `wiki_query`, `wiki_analysis`, `wiki_lint`) with structured JSON output, stdio transport, behavior differences from CLI (no prompts, no rich output, stale warning in structured output), shared-core-logic constraint. Added FR-10 (help command): `wiki help` overview table, `wiki help <command>` per-command pages, `wiki help mcp` with runtime-resolved vault path and `.mcp.json` snippet. Updated FR-1 to reference five subcommands and two entry points. Updated pyproject.toml structure to document `wiki-mcp` script. Added `mcp` Python SDK to External Dependencies. Added acceptance tests #16 and #17. Added "MCP authentication" and "multi-vault MCP server" to Out of Scope. Added "MCP server", "MCP tool", "stdio transport" to Glossary. Updated Explicit Constraints to replace blanket "no networked interface" with MCP-aware version and add no-duplicate-logic constraint. |
| 1.4.0 | 2026-04-27 | Architect Agent | Added FR-3.8 (staleness detection in ingest): change-set tracking, stale banner insertion, ` ⚠ stale` index annotation, `query-stale` log entry, terminal summary. Added staleness nag at startup of FR-4 (analysis) and FR-5 (query). Added FR-8 (lint command) with Part 1 staleness resolution and Part 2 deep health-check (orphans, missing cross-refs, contradictions, concept gaps), `lint-report.md` output, and empty-vault/exit-code behavior. Added `lint-report.md` to vault layout and data model. Updated FR-1 subcommand count to 4. Updated FR-6.1 to document `query-stale` and `lint-resolved` log entry types. Updated exit code table to include `lint`. Updated data flow. Added user workflow scenarios for stale flagging and lint. Added acceptance tests #14 and #15. Added CLI subcommand `lint`. Updated glossary with "Stale query page" and "Lint". |
| 1.3.0 | 2026-04-27 | Architect Agent | Added query answer persistence to FR-5: post-answer save prompt, slugified filename, `queries/` subdirectory, `index.md` entry, `query-saved` log entry, numeric suffix deduplication. Added `queries/` to vault layout. Updated FR-3.7 exclusion list and data model table. Added DO NOT constraint for slug collision. Added user workflow scenario "Save a query answer to the wiki". Added acceptance test #13. Updated glossary with "Saved query page" and "Slug". |
| 1.2.0 | 2026-04-27 | Architect Agent | Incorporated NQ-1 through NQ-3 resolutions: hardcoded `ANALYSIS_CONTEXT_WINDOW = 64_000` constant added to Technical Context and FR-4; `QUERY_CONTEXT_WINDOW = 128_000` constant added to Technical Context and FR-5; query LLM response array is now sorted by relevance descending (re-ranking); `## Sources` trailing section with markdown list mandated in FR-5 and acceptance test #7; oversized-file skip behavior and budget-overflow truncation with "X additional relevant files" note added to FR-5; acceptance tests #11 and #12 added for query overflow edge cases |
| 1.1.0 | 2026-04-27 | Architect Agent | Incorporated Q-6 through Q-15 resolutions from Critical Review #1: summary naming (`<name>.<ext>.md`), tiktoken-based analysis batching, active-vault Obsidian CLI, LLM-driven reference detection with `(inferred)` annotation, tiktoken query retrieval, removed performance NFR, added exit code table, config validation on startup, MD5-based acceptance test #2, empty vault error for analysis/query; also mandated index.md table format, UTC timestamps in log.md, JSON array format for query LLM response, and marked FR-7 as exploratory |
| 1.0.0 | 2026-04-27 | Architect Agent | Initial specification |
