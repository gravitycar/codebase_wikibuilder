# Specification: Query Cache — Have I Answered This Before?

## Document Metadata
- **Version**: 1.4.0
- **Author**: Architect Agent (MAPS)
- **Created**: 2026-05-08
- **Status**: Draft
- **Related Specs**: `.maps/docs/codebase-wiki-builder/specification/spec.md` (base system)

---

## Executive Summary

Add a pre-check to the query workflow that detects when an existing saved query page already answers the user's question, and surfaces it before running the full two-LLM-call workflow. The check uses a two-stage approach: (1) a fast slug/title normalization pass against saved files on disk, then (2) an LLM pre-check against existing query page titles and one-line summaries from `index.md`. If a non-stale match is found, the cached answer is returned to the caller with attribution metadata — skipping both LLM calls and saving cost and latency. Stale matches are treated as cache misses and bypassed. The feature applies uniformly to both the CLI `query` command and the MCP `wiki_query` tool.

---

## User Story

As a developer (or AI agent) asking repeated or similar questions against the same codebase wiki, I want the system to recognize when it has already answered my question and return the saved answer immediately, so that I avoid unnecessary LLM API calls, reduce latency, and do not accumulate duplicate query pages.

---

## Stakeholders

- **Primary**: Individual developers running `codewiki query` interactively
- **Secondary**: AI coding agents calling the MCP `wiki_query` tool programmatically
- **Tertiary**: None (local single-user tool)

---

## Success Criteria

1. **Slug-hit suppresses LLM calls**: When a saved query page exists whose slugified question matches the incoming question (normalized), the system returns the cached answer without calling `llm_client.complete()`.
2. **LLM pre-check catches title-close questions**: When the slug-hit misses but an existing query page's title is semantically equivalent to the incoming question (as determined by the LLM pre-check), the system returns the cached answer.
3. **Stale pages are always bypassed**: A saved query page marked stale (via the `> [!warning] Stale Content` banner) is never returned from cache; the full two-LLM-call workflow runs instead.
4. **MCP does not create duplicate files**: When the cache returns a hit for a question already saved in `queries/`, `save_query_page()` is not called, preventing a duplicate `-2.md` file from being created.
5. **CLI shows cache attribution**: CLI cache hits display a note identifying which saved page was used and when it was saved, before printing the answer.
6. **MCP returns all fields always**: Every MCP `wiki_query` response includes all 6 fields (`answer`, `saved_path`, `stale_warnings`, `cache_hit`, `cached_at`, `one_line_summary`). Cache hits have `cache_hit: true` and a non-null `cached_at`; fresh results have `cache_hit: false` and `cached_at: null`.
7. **`QueryResult` carries cache signal**: The `from_cache` field on `QueryResult` is `True` for cache hits and `False` (default) for fresh results. All callers use this field to suppress or enable the save step.

---

## Context and Problem Statement

### Current State

Every call to `run_query()` unconditionally executes two LLM calls: one to identify relevant summary files from `index.md`, and one to generate an answer from those summaries. When a developer or AI agent asks the same (or very similar) question multiple times, the full pipeline reruns on every call, incurring cost and latency, and — in MCP mode — creating a new `queries/<slug>-N.md` file for each duplicate.

### Desired State

A lightweight pre-check at the entry point of `run_query()` that:
1. Detects if the incoming question matches an existing, non-stale saved query page.
2. Returns the cached answer immediately if found, skipping both LLM calls.
3. Surfaces attribution information to both CLI and MCP callers.
4. Prevents MCP from writing a duplicate query page for a question it has already answered.

---

## Functional Requirements

### FR-QC-1: Cache Module

The system SHALL implement a dedicated cache module (`query_cache.py`) responsible for all cache lookup logic. The module SHALL expose a single public function used by `run_query()` to attempt a cache hit before executing the two-LLM-call pipeline.

The public function signature SHALL be:

```
check_query_cache(
    question: str,
    vault_root: Path,
    index_content: str,
    llm_client: LLMClient,
    config: WikiConfig,
) -> QueryResult | None
```

Returns `None` on a cache miss. Returns a `QueryResult` with `from_cache=True` on a cache hit. `query_cache.py` imports and calls `has_stale_banner()` directly from `staleness.py`.

### FR-QC-2: Two-Stage Lookup

The cache lookup SHALL proceed in two stages in sequence:

#### Stage 1 — Slug Normalization Pass

1. Apply `slugify()` directly to the incoming question (no pre-normalization before slugifying) to produce a candidate slug.
2. If `slugify(question)` produces an empty string, skip Stage 1 entirely and fall through directly to Stage 2. Do NOT apply any fallback slug value (such as `"query"`) — that fallback is used only by `_make_slug()` at save time, not during lookup.
3. Scan for candidate files `queries/<slug>.md`, `queries/<slug>-2.md`, `queries/<slug>-3.md`, ... (continuing until no file is found at the next numeric suffix).
4. For each candidate file found:
   a. Parse the file using `read_query_page()` to extract the stored question (H1 title).
   b. Normalize both the stored question and the incoming question (lowercase, strip punctuation, collapse whitespace) and compare. If they match, proceed to the staleness check (FR-QC-3).
   c. If no match, continue to the next candidate file.
5. If Stage 1 produces a match that passes the staleness check, return the cached result immediately (Stage 2 is skipped).
6. If Stage 1 finds no non-stale match, proceed to Stage 2.

#### Stage 2 — LLM Pre-Check

1. Parse all query page rows from `index.md`: collect (wikilink target, one-line description) pairs for all rows whose wikilink path begins with `queries/`.
2. If no query page rows exist, return a cache miss immediately.
3. Read the actual H1 title from each query page file on disk (by parsing the file with `read_query_page()`). Files that fail to parse are skipped (not a hard error). Construct a prompt instructing the LLM to compare the incoming question against the list of real H1 question titles (not de-slugified paths) and their one-line descriptions from `index.md`.
4. The prompt SHALL instruct the LLM to return either:
   - The wikilink path of the single best-matching existing page (e.g., `queries/how-does-auth-work`) if it judges the existing page substantially answers the incoming question, OR
   - A sentinel value indicating no match (e.g., `none`).
5. If the LLM returns a matching path, validate the path before opening any file on disk (SEC-3 path traversal fix). Perform all three checks in sequence; if any check fails, treat the result as a cache miss immediately — do not raise an error:
   i. **Prefix check**: The returned path string must begin with `queries/`.
   ii. **Containment check**: The resolved absolute path (`Path(vault_root / path).resolve()`) must be within `vault_root / "queries/"`, verified using `Path.is_relative_to()`.
   iii. **Allowlist check**: The returned path (without the `.md` suffix) must be a member of the pre-computed set of valid wikilink targets already parsed from `index.md` (the same set used to build the Stage 2 prompt in step 1).
   a. `index.md` already stores full vault-relative wikilinks including numeric suffixes (e.g. `[[queries/how-does-auth-work-2]]`), so the returned path already encodes the exact file. Locate the file at `<vault>/<returned-path>.md` (appending `.md` to the wikilink path). Each row in `index.md` is a distinct file entry, and the LLM returns the specific path — no further suffix iteration is needed.
   b. Parse the file using `read_query_page()`.
   c. Proceed to the staleness check (FR-QC-3).
   d. If the file is stale, return a full cache miss immediately. Do NOT attempt sibling fallback (e.g. `-2`, `-3` variants) — a stale Stage 2 match is always a miss.
   e. If not stale, return the cached result.
6. If the LLM returns no match, return a cache miss.

### FR-QC-3: Staleness Check

Before returning any candidate page as a cache hit, the system SHALL verify the page is not stale:

1. Call `has_stale_banner(raw_content)` (from `staleness.py`, a public function) on the parsed page's raw content.
2. If the stale banner is present, the candidate is disqualified. This counts as a cache miss for that candidate.
3. If no stale banner is present, the candidate is valid for return.

The staleness check is the same regardless of which stage produced the candidate.

### FR-QC-4: QueryResult Extension

The `QueryResult` dataclass SHALL be extended with three new fields:

- `from_cache: bool` — `True` if the result was returned from a saved page without running LLM calls; `False` (the default) for all fresh results.
- `cached_path: Path | None` — vault-relative path of the matched cache page (e.g. `queries/how-does-auth-work.md`); `None` on fresh (non-cached) queries. Populated by `check_query_cache()` on a cache hit.
- `cached_at: str | None` — the `saved_at` timestamp string from the matched page's `## Page Metadata` section; `None` on fresh (non-cached) queries. Populated by `check_query_cache()` on a cache hit. If `saved_at` is absent or unparseable in the matched page, `cached_at` SHALL be `None` — a missing timestamp is not grounds for a cache miss.

No existing fields are added or removed. The `answer`, `sources`, `one_line_summary`, and `stale_warnings` fields retain their existing semantics. For cache hits:
- `answer` SHALL be the full raw content read verbatim from the saved page file — answer body plus `## Sources` section — exactly as originally written. No reconstruction is performed.
- `sources` SHALL be the list of vault-relative paths parsed from the page's `## Sources` section.
- `one_line_summary` SHALL be the one-line description from the matching `index.md` row for that page.
- `stale_warnings` SHALL be the existing stale warnings collected by `_collect_stale_warnings(index_content)` (already computed at the top of `run_query()` before the cache check runs).

### FR-QC-5: Integration Point in run_query()

The cache pre-check SHALL be inserted inside `run_query()` in `query_engine.py`, after `index.md` is read and `_collect_stale_warnings()` is called, but before the first LLM call. Both CLI and MCP callers benefit uniformly through this single integration point.

The integration follows this sequence:
1. Read `index.md` (existing step).
2. Call `_collect_stale_warnings(index_content)` (existing step).
3. **[NEW]** Call `check_query_cache()`. If it returns a valid `QueryResult`, `check_query_cache()` has already set `from_cache=True` on the returned result; return it immediately. Skip all remaining steps.
4. Continue with the existing two-LLM-call pipeline (existing step).

### FR-QC-6: CLI Behavior on Cache Hit

When `run_query()` returns a `QueryResult` with `from_cache=True`, `_run_query_command()` in `cli.py` SHALL:

1. Print a cache attribution line to the terminal before the answer, in the format:
   `[cache] Answering from saved page: <vault-relative path> (saved <saved_at timestamp>)`
2. Print the answer body as normal.
3. Print stale-page warnings as normal (if any exist from `stale_warnings`).
4. Skip the save prompt (`_prompt_save()`). A cache hit means the page already exists; there is nothing to save.
5. Still append a `query` log entry to `log.md` by calling `write_query_log_entry()` with `cache_hit=True` (see FR-QC-7a). The log entry format is the same as for fresh queries, with a `cache-hit` marker included in the entry.

### FR-QC-7: MCP Behavior on Cache Hit

When `run_query()` returns a `QueryResult` with `from_cache=True`, `_handle_wiki_query()` in `mcp_server.py` SHALL:

1. Skip the call to `save_query_page()`. The `from_cache` field is the signal that suppresses saving.
2. Include `cache_hit: true` in the JSON response.
3. Include `cached_at: <saved_at timestamp>` in the JSON response. The `saved_at` value is taken from `QueryResult.cached_at` (populated by `check_query_cache()`).
4. Include `saved_path` in the JSON response set to the vault-relative path of the matched page (e.g., `"queries/how-does-auth-work.md"`). This value is taken from `QueryResult.cached_path`.
5. Return `answer`, `one_line_summary`, and `stale_warnings` fields as normal.
6. Write a `query` log entry to `log.md` for cache hits by calling `write_query_log_entry()` (see FR-QC-7a) with `cache_hit=True`.

The MCP `wiki_query` response schema SHALL include all 6 fields in every response (both cache hits and fresh results):
```
{
  "answer": str,
  "saved_path": str,
  "stale_warnings": list[str],
  "cache_hit": bool,
  "cached_at": str | null,
  "one_line_summary": str
}
```
All 6 fields SHALL always be present. `cache_hit` SHALL be `false` and `cached_at` SHALL be `null` on fresh (non-cached) responses. `cached_at` SHALL be the `saved_at` timestamp string from the matched page's `## Page Metadata` on cache hits. If `saved_at` is missing or unparseable on a cache hit, `cached_at` SHALL be `null`; the response is still a cache hit (`cache_hit: true`) — a missing timestamp does not invalidate the cache entry.

### FR-QC-7a: Standalone Log Helper

A standalone helper function SHALL be extracted into `query_persistence.py` with the following signature:

```
write_query_log_entry(
    question: str,
    vault_root: Path,
    log_fn: Callable[[str], None],
    cache_hit: bool = False,
) -> None
```

This helper encapsulates all log-write logic for `log.md` query entries. Both `save_query_page()` (with `cache_hit=False`) and the MCP cache-hit path in `_handle_wiki_query()` (with `cache_hit=True`) SHALL call this helper directly rather than duplicating log-write code. The log entry format is identical for both callers; cache-hit entries use `cache-hit` as the entry type field, e.g. `{timestamp} | cache-hit | {question} | {cached_path}`. Fresh query entries do not include the marker.

### FR-QC-8: Cache Miss Fallthrough

If both stages return a cache miss, `run_query()` SHALL proceed with the full two-LLM-call pipeline exactly as before. The cache logic is entirely transparent on a miss — no error is raised, no message is printed.

### FR-QC-9: Error Handling in Cache Lookup

The cache lookup function SHALL handle errors defensively:

1. If `read_query_page()` raises any exception for a candidate file, that candidate is skipped (treated as a cache miss for that candidate). The exception SHALL be logged at DEBUG level.
2. If the Stage 2 LLM call raises `LLMError`, the entire cache lookup SHALL return a miss and `run_query()` SHALL proceed with the full pipeline. The exception SHALL be logged at WARNING level.
3. The cache lookup SHALL never raise an exception to its caller. All errors result in a cache miss.

---

## Non-Functional Requirements

### Latency Budget

- **Stage 1 (slug pass)**: SHALL complete in under 50 ms for vaults with up to 500 saved query pages (pure filesystem + string operations; no LLM call).
- **Stage 2 (LLM pre-check)**: Adds one LLM call overhead. This is acceptable because a Stage 2 hit still saves the second (more expensive) answer-generation LLM call. Stage 2 SHALL only run when Stage 1 finds no match.
- **Cache miss overhead**: Stage 1 adds negligible latency to the existing pipeline. Stage 2 adds one LLM round-trip only when Stage 1 misses and query pages exist. The net cost on a full miss is one extra LLM call.

### Token Cost

- Stage 1 consumes no LLM tokens.
- Stage 2 consumes tokens proportional to the number of query page rows in `index.md` (titles + one-line summaries only — not full page content). This is a cheap call by design.
- On a cache hit (either stage), the two existing LLM calls are eliminated entirely, resulting in net token savings for any repeated question.

### Correctness

- The cache SHALL NOT return a stale page under any circumstances. Stale check is mandatory.
- The cache SHALL NOT return a page for a different question. The Stage 1 normalization comparison must be an exact match after normalization; Stage 2 defers to LLM judgment with conservative instructions.
- The `from_cache` field on `QueryResult` is the authoritative signal for all downstream callers. No caller SHALL use heuristics to infer cache status independently.

---

## Explicit Constraints (DO NOT)

- Do NOT use embedding-based (vector) similarity for the cache check in this version. The two-stage slug + LLM approach is sufficient for v1.
- Do NOT implement a TTL (time-to-live) mechanism. Staleness is already managed by `staleness.py`'s event-driven invalidation; no time-based expiry is needed.
- Do NOT return a stale page from cache under any circumstances. The stale check is non-negotiable.
- Do NOT call `save_query_page()` when `from_cache=True`. Saving would create a duplicate file with a `-2` suffix.
- Do NOT modify the slug deduplication scheme (`slug-2.md`, `slug-3.md`, etc.) in `query_persistence.py`. The cache lookup must walk the existing sequence but preserve its behavior.
- Do NOT add a `save` parameter or cache-bypass parameter to the `wiki_query` MCP tool. The MCP interface is not extended; only its JSON response schema gains new fields.
- Do NOT expose the cache lookup function as a public CLI subcommand or MCP tool.
- Do NOT add new Python package dependencies. The implementation uses only existing project dependencies and the Python standard library.
- Do NOT duplicate the cache lookup logic between CLI and MCP. The single integration point inside `run_query()` handles both callers.
- Do NOT skip the `log.md` entry for cache hits. Cache hits SHALL still produce a `query` log entry (with a `cache-hit` marker) — the same as fresh queries.

---

## Technical Context

### Relevant Existing Code

The following modules are directly involved in this feature. Implementers should read them before building:

**`query_engine.py`**: Contains `run_query()` and `QueryResult`. The cache pre-check is inserted at the top of `run_query()` after step 2 (`_collect_stale_warnings`). The `QueryResult` dataclass gains `from_cache: bool`.

**`query_persistence.py`**: Contains `read_query_page()` (parses a saved `.md` file into `QueryPage` with `question`, `answer_body`, `sources`, `saved_at`, `updated_at`, `raw_content`) and `_make_slug()` / `_unique_query_path()` (slug dedup logic). The cache lookup reuses `read_query_page()` to load candidate pages. Also gains a new public helper `write_query_log_entry(question, vault_root, log_fn, cache_hit=False)` that encapsulates all log-write logic for `log.md` query entries; called by both `save_query_page()` and the MCP cache-hit path.

**`staleness.py`**: Contains `has_stale_banner(content: str) -> bool` (public function; checks for `> [!warning] Stale Content` callout). `query_cache.py` imports and calls this directly. The cache lookup calls this on every candidate page.

**`vault.py`**: Contains `slugify(text: str) -> str` (lowercase → replace spaces with hyphens → strip non-alphanumeric → collapse hyphens → strip leading/trailing hyphens). The cache lookup applies the same normalization as `_make_slug()` in `query_persistence.py` to produce the lookup key.

**`index_writer.py`**: Contains `parse_existing_index()` (builds a `{wikilink_target: description}` dict from `index.md`). Stage 2 reads query page rows from `index.md` using this existing parse; the one-line summary is the description value for a given `queries/<slug>` wikilink target.

**`llm_client.py`**: Contains `LLMClient.complete(prompt: str) -> str`. Stage 2 uses one call to `complete()` with a prompt listing existing query page titles and descriptions. The same retry and error behavior as existing LLM calls applies.

**`cli.py`**: `_run_query_command()` handles CLI output. It is modified to print the cache attribution line and skip the save prompt when `result.from_cache` is `True`.

**`mcp_server.py`**: `_handle_wiki_query()` is modified to skip `save_query_page()` and include `cache_hit` / `cached_at` in the JSON response when `result.from_cache` is `True`.

### Slug Lookup Sequence

The slug dedup scheme in `_unique_query_path()` produces filenames in the sequence: `<slug>.md`, `<slug>-2.md`, `<slug>-3.md`, ... The cache Stage 1 lookup MUST walk this same sequence: check `queries/<slug>.md` first, then `queries/<slug>-2.md`, continuing until a numeric suffix produces a path that does not exist on disk. All files found in the sequence are candidates; the first one whose stored question (normalized) matches the incoming question (normalized) wins.

### Normalization for Stage 1 Comparison

Stage 1 calls `slugify(question)` directly on the raw incoming question to produce the lookup slug — there is no pre-normalization step applied before slugifying.

The normalization (lowercase, strip punctuation, collapse whitespace) applies only to the H1 text comparison step, not to computing the lookup slug. Both the incoming question and the stored H1 question SHALL be normalized identically before text comparison:
- Convert to lowercase
- Strip all punctuation characters (characters that are not alphanumeric or whitespace)
- Collapse all runs of whitespace to a single space
- Strip leading and trailing whitespace

The comparison is an exact string equality check on the normalized forms. This is not fuzzy matching.

### index.md Row Format for Stage 2

Stage 2 reads query page rows from `index.md`. A query page row has the form:
```
| [[queries/<slug>]] | <one_line_summary> |
```
The wikilink target is `queries/<slug>` (no `.md` extension, per Obsidian convention). The description may have a ` ⚠ stale` suffix for stale pages. Stage 2 SHALL include stale-annotated rows in the prompt (to allow the LLM to identify them as candidates), but the staleness check (FR-QC-3) will disqualify them before a cached result is returned.

The Stage 2 LLM prompt SHALL present each row as a (question title, one-line description) pair. The question title is the actual H1 title read from each `queries/*.md` file on disk (via `read_query_page()`), not a de-slugified wikilink path. The LLM therefore receives real question text for its comparison, which produces more accurate matching. Files that fail to parse are skipped (not a hard error).

### QueryPage Struct (from read_query_page)

The `QueryPage` dataclass returned by `read_query_page()` contains:
- `path: Path` — absolute path to the file
- `question: str` — H1 title (the original question)
- `answer_body: str` — content between H1 and `## Sources`
- `sources: list[str]` — vault-relative paths from `## Sources`
- `saved_at: str` — timestamp string from `## Page Metadata`
- `updated_at: str` — timestamp string from `## Page Metadata`
- `raw_content: str` — full file content (used for stale banner check)

### Reconstructing QueryResult from Cache

A cached `QueryResult` is built from the `QueryPage` as follows:
- `answer`: the full raw content read verbatim from the saved page file — answer body plus `## Sources` section — exactly as originally written. No reconstruction from `answer_body` is performed.
- `sources`: taken directly from `QueryPage.sources`
- `one_line_summary`: taken from the description column of the matching `index.md` row (strip any ` ⚠ stale` suffix if present, though a stale page should never reach this point)
- `stale_warnings`: taken from `_collect_stale_warnings(index_content)` already computed in `run_query()` before the cache check — passed through unchanged
- `from_cache`: `True`

---

## Data Requirements

### QueryResult Schema Change

| Field | Type | Change |
|-------|------|--------|
| `answer` | `str` | Unchanged |
| `sources` | `list[str]` | Unchanged |
| `one_line_summary` | `str` | Unchanged |
| `stale_warnings` | `list[str]` | Unchanged |
| `from_cache` | `bool` | **NEW** — default `False` |
| `cached_path` | `Path \| None` | **NEW** — default `None`; vault-relative path of matched cache page on cache hits |
| `cached_at` | `str \| None` | **NEW** — default `None`; `saved_at` timestamp string from matched page on cache hits; `None` if `saved_at` is missing or unparseable |

### MCP wiki_query Response Schema Change

| Field | Type | Change |
|-------|------|--------|
| `answer` | `str` | Unchanged — verbatim raw file content on cache hits |
| `saved_path` | `str` | Unchanged (on cache hit: path of the matched existing page) |
| `stale_warnings` | `list[str]` | Unchanged |
| `cache_hit` | `bool` | **NEW** — `false` on fresh results, `true` on cache hits |
| `cached_at` | `str \| null` | **NEW** — `null` on fresh results, `saved_at` timestamp on cache hits |
| `one_line_summary` | `str` | **NEW** — always present; from `index.md` description row |

All 6 fields SHALL always be present in every `wiki_query` response. No existing fields are removed.

---

## User Workflows

**Scenario: Same question asked twice (CLI)**
- **Given**: The vault has a saved page `queries/how-does-auth-work.md` (not stale)
- **When**: The user runs `codewiki query "How does auth work?"`
- **Then**: Stage 1 slug match fires; CLI prints `[cache] Answering from saved page: queries/how-does-auth-work.md (saved 2026-04-29 10:00:00 UTC)` followed by the saved answer; no LLM calls are made; no save prompt is shown; a query log entry is still appended

**Scenario: Same question asked twice (MCP)**
- **Given**: The vault has a saved page `queries/how-does-auth-work.md` (not stale)
- **When**: An AI agent calls `wiki_query` with `{"question": "How does auth work?"}`
- **Then**: Stage 1 slug match fires; `save_query_page()` is NOT called; JSON response includes `"cache_hit": true, "cached_at": "2026-04-29 10:00:00 UTC"`; no duplicate file is created

**Scenario: Question rephrased (LLM pre-check)**
- **Given**: The vault has a saved page `queries/how-does-auth-work.md` (not stale, description: "Explains authentication middleware")
- **When**: The user runs `codewiki query "Explain how authentication is handled"`
- **Then**: Stage 1 slug miss; Stage 2 LLM pre-check identifies `queries/how-does-auth-work` as a match; staleness check passes; CLI prints cache attribution and the saved answer

**Scenario: Stale page bypassed**
- **Given**: The vault has `queries/how-does-auth-work.md` with a `> [!warning] Stale Content` banner
- **When**: The user runs `codewiki query "How does auth work?"`
- **Then**: Stage 1 slug match fires, staleness check fails; cache returns a miss; the full two-LLM-call pipeline runs; a fresh answer is produced

**Scenario: No saved query pages exist**
- **Given**: The vault has no files under `queries/`
- **When**: The user runs `codewiki query "How does auth work?"`
- **Then**: Stage 1 finds no candidate files; Stage 2 finds no query rows in `index.md` and skips the LLM call immediately; the full pipeline runs as before

---

## Acceptance Tests

1. **Stage 1 slug hit — CLI**: Given a vault with `queries/how-does-auth-work.md` (not stale, H1: "How does auth work?"). Run `codewiki query "How does auth work?"`. Verify: (a) the CLI output begins with a `[cache] Answering from saved page: queries/how-does-auth-work.md` line; (b) no new file is created under `queries/`; (c) `log.md` gains a query entry; (d) exactly zero new LLM calls are made (mock the LLM client and assert zero calls to `complete()`).

2. **Stage 1 slug hit — normalization**: Given a vault with `queries/how-does-auth-work.md` (not stale, H1: "How does auth work?"). Run `codewiki query "HOW DOES AUTH WORK?"`. Verify: the Stage 1 pass matches the stored page and returns the cached answer (same checks as test 1).

3. **Stage 1 slug hit with numeric suffix**: Given a vault with `queries/how-does-auth-work.md` (H1: "Some other question") and `queries/how-does-auth-work-2.md` (H1: "How does auth work?", not stale). Run `codewiki query "How does auth work?"`. Verify: (a) the cache returns the answer from `how-does-auth-work-2.md`; (b) no new file is created.

4. **Stale page bypass**: Given a vault with `queries/how-does-auth-work.md` that has a `> [!warning] Stale Content` banner. Run `codewiki query "How does auth work?"`. Verify: (a) no `[cache]` attribution line appears in CLI output; (b) the LLM client's `complete()` is called exactly twice (full pipeline runs); (c) a new file is created in `queries/` (MCP mode) or the user is prompted to save (CLI mode).

5. **Stage 2 LLM pre-check hit**: Given a vault with `queries/how-does-auth-work.md` (not stale). Mock Stage 1 to miss (slug does not match). Configure the LLM pre-check mock to return `queries/how-does-auth-work`. Verify: the answer is returned from cache; `complete()` is called exactly once (for the pre-check, not the full pipeline).

6. **Stage 2 miss — full pipeline runs**: Given a vault with `queries/how-does-auth-work.md`. Mock Stage 1 to miss and the LLM pre-check to return `none`. Verify: `complete()` is called exactly three times total (one for Stage 2 pre-check, two for the full pipeline).

7. **Stage 2 skipped when no query pages exist**: Given a vault with no files under `queries/` and no query rows in `index.md`. Run `codewiki query "Anything?"`. Verify: Stage 2 does not call `complete()` at all (only the two full-pipeline calls are made).

8. **MCP cache hit — no duplicate file**: Given a vault with `queries/how-does-auth-work.md` (not stale, H1: "How does auth work?"). Call `wiki_query` via MCP with `{"question": "How does auth work?"}`. Verify: (a) no new file is created under `queries/`; (b) the JSON response includes `"cache_hit": true`; (c) `cached_at` is a non-empty timestamp string; (d) `saved_path` is `"queries/how-does-auth-work.md"`.

9. **MCP fresh result — cache_hit false**: Given a vault with no matching saved page. Call `wiki_query` via MCP. Verify: the JSON response includes `"cache_hit": false` and `"cached_at": null`.

10. **from_cache field default**: Construct a `QueryResult` without specifying `from_cache`. Verify: `from_cache` is `False` by default (does not break existing code that constructs `QueryResult`).

11. **Cache lookup error resilience**: Mock `read_query_page()` to raise an exception for a candidate file. Run `codewiki query` with a matching slug. Verify: (a) no exception propagates to the caller; (b) the full pipeline runs as a fallback; (c) the exception is logged at DEBUG level.

12. **Stage 2 LLM error resilience**: Mock the Stage 2 LLM call (`complete()`) to raise `LLMError`. Run `codewiki query`. Verify: (a) no exception propagates; (b) the full two-LLM-call pipeline runs; (c) the `LLMError` is logged at WARNING level.

13. **CLI save prompt suppressed on cache hit**: Given a vault with a matching non-stale saved page. Run `codewiki query "How does auth work?"` interactively. Verify: the save prompt (`Save this answer to the wiki? [y/N]`) does NOT appear.

14. **Stale warnings still surfaced on cache hit**: Given a vault with a non-stale matching saved page AND other stale pages listed in `index.md`. Run `codewiki query`. Verify: stale-page warnings for the OTHER stale pages are still printed to the CLI (as they would be in the non-cache path).

15. **SEC-3 prefix check — path traversal rejected**: Mock the Stage 2 LLM to return a path that does NOT begin with `queries/` (e.g., `../sensitive/file` or `summaries/some-page`). Verify: (a) no file is opened on disk; (b) the result is treated as a cache miss; (c) the full two-LLM-call pipeline runs; (d) no exception propagates to the caller.

16. **SEC-3 containment check — path traversal rejected**: Mock the Stage 2 LLM to return a path that begins with `queries/` but resolves outside `vault_root/queries/` after `Path.resolve()` (e.g., `queries/../../etc/passwd`). Verify: (a) no file is opened on disk; (b) the result is treated as a cache miss; (c) the full pipeline runs; (d) no exception propagates.

17. **SEC-3 allowlist check — unlisted path rejected**: Mock the Stage 2 LLM to return a syntactically valid path (begins with `queries/`, resolves within `vault_root/queries/`) that is NOT present in the pre-computed set of wikilink targets from `index.md`. Verify: (a) no file is opened on disk; (b) the result is treated as a cache miss; (c) the full pipeline runs; (d) no exception propagates.

---

## Open Questions

None — all resolved.

### Resolution Log

**OQ-1 (Stage 2 model selection) — Resolved v1.3.0**: `check_query_cache()` receives the `llm_client` parameter (the same configured `LLMClient` used for all other LLM calls in the system) and uses it directly for the Stage 2 pre-check. No separate cheaper model is used; no new configuration key is introduced.

**OQ-2 (Stage 2 prompt conservatism) — Resolved v1.3.0**: The Stage 2 prompt SHALL be very conservative. A cache hit is only declared when the LLM is highly confident that BOTH of the following conditions are met simultaneously:
1. The stored question is a strong match to the incoming question.
2. The existing answer completely answers the incoming question.

The prompt wording SHALL reflect both conditions explicitly, instructing the LLM to return a match only when highly confident on both dimensions. False negatives (missing a valid cache hit) are explicitly preferred over false positives (returning an incomplete or mismatched cached answer).

**OQ-3 (`saved_at` fallback for `cached_at`) — Resolved v1.3.0**: If a cached page's `saved_at` field is missing or unparseable, `cached_at` SHALL be `null` in both the `QueryResult` and the MCP response. A missing or unparseable `saved_at` is NOT grounds for a cache miss — the cache entry is still returned as a hit; only `cached_at` is `null`.

---

## Dependencies

### Upstream Dependencies
- Python 3.12+ runtime
- Anthropic or OpenAI API access (for Stage 2 LLM call)
- Existing codebase: `query_engine.py`, `query_persistence.py`, `staleness.py`, `vault.py`, `index_writer.py`, `cli.py`, `mcp_server.py`

### Downstream Impact
- Any consumer of `QueryResult` (CLI, MCP, tests) must tolerate the new `from_cache` field. Because `from_cache` defaults to `False`, existing code that constructs `QueryResult` without specifying `from_cache` is unaffected.
- The MCP `wiki_query` response schema gains three new fields (`cache_hit`, `cached_at`, `one_line_summary`), all of which are always present. Callers that do not check for these fields are unaffected; callers that do can use them for attribution and display.

---

## Out of Scope

- Embedding-based (vector) semantic similarity matching (deferred to a future version)
- TTL-based cache expiry (not needed; staleness is managed by `staleness.py`)
- A `--force-refresh` or `--no-cache` flag on the CLI or MCP tool (future enhancement)
- Cache metrics or hit-rate logging (future enhancement)
- Proactive cache warming or pre-computation
- Multi-vault cache sharing

---

## Risks and Mitigations

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| Stage 2 false positive (wrong answer returned) | Low | High | Conservative LLM prompt; stale check as backstop; bias toward cache misses |
| Stage 2 LLM error blocks query | Low | High | LLM errors in Stage 2 fall back to cache miss silently; full pipeline runs |
| Slug collision without exact-match guard | Low | Medium | Stage 1 always compares normalized question text, not just slug; N-suffix scan covers all candidates |
| MCP duplicate file creation | Medium | Medium | `from_cache` field suppresses `save_query_page()` in MCP handler; acceptance test AT-8 verifies |
| `read_query_page()` parse failure | Low | Low | Exception caught and logged; candidate skipped; full pipeline runs |

---

## Appendices

### Glossary

- **Cache hit**: A cache lookup that finds a non-stale saved query page matching the incoming question, allowing the answer to be returned without running LLM calls.
- **Cache miss**: A cache lookup that finds no match or finds only stale candidates. The full two-LLM-call pipeline runs.
- **Stage 1 (slug pass)**: The first stage of the cache lookup: fast filesystem-based slug normalization and exact-match comparison against saved query page H1 titles.
- **Stage 2 (LLM pre-check)**: The second stage: a single LLM call that compares the incoming question against existing query page titles and descriptions from `index.md`.
- **Staleness check**: The mandatory check that disqualifies any candidate page bearing the `> [!warning] Stale Content` banner from being returned as a cache hit.
- **`from_cache`**: The `bool` field added to `QueryResult` that signals to callers (CLI, MCP) that the result came from a saved page rather than the live LLM pipeline.
- **`cache_hit` / `cached_at`**: New fields in the MCP `wiki_query` JSON response that surface cache status and the original save timestamp to MCP callers.

### Version History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.4.0 | 2026-05-08 | Architect Agent | SEC-3 path traversal fix: add mandatory 3-part path validation step (prefix check, containment check, allowlist check) in FR-QC-2 Stage 2 immediately after LLM response and before any file is opened; validation failures are silent cache misses. Add acceptance tests AT-15, AT-16, AT-17 covering each SEC-3 check. |
| 1.3.0 | 2026-05-08 | Architect Agent | Resolve all open questions: OQ-1 (same llm_client for Stage 2); OQ-2 (conservative Stage 2 prompt, both conditions explicit, false negatives preferred); OQ-3 (cached_at=null if saved_at missing, not a miss). Incorporate critic observations: OBS-1 (log_fn typed as Callable[[str], None]); OBS-2 (FR-QC-5 step 3 credits check_query_cache() for from_cache=True); OBS-3 (cache-hit log entry format specified as {timestamp} \| cache-hit \| {question} \| {cached_path}) |
| 1.2.0 | 2026-05-08 | Architect Agent | Incorporate Round 2 Q&A: `cached_path` and `cached_at` added to `QueryResult` (NQ-1); `write_query_log_entry()` standalone helper in `query_persistence.py` (NQ-2); `_parse_existing_index()` renamed to `parse_existing_index()` public (NQ-3); Stage 2 reads actual H1 titles from disk (normative), OQ-1 removed (NQ-4) |
| 1.1.0 | 2026-05-08 | Architect Agent | Incorporate Round 1 Q&A: Stage 1 calls slugify() directly (Q-1); stale primary = full miss, no sibling fallback (Q-2); clarifying note on index.md numeric suffix wikilinks (Q-3); `_has_stale_banner` renamed to `has_stale_banner` public (Q-4); empty slug skips Stage 1 entirely (Q-5); MCP log entry for cache hits with cache-hit marker (Q-6); all 6 MCP fields always present (Q-7); `check_query_cache` function signature defined (Q-8); answer field is verbatim raw file content (Q-9) |
| 1.0.0 | 2026-05-08 | Architect Agent | Initial specification |
