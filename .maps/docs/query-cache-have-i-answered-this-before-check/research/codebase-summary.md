# Codebase Summary: Query Cache — Have I Answered This Before?

## Tech Stack

- **Language**: Python 3.12+
- **CLI framework**: Typer + Rich
- **MCP SDK**: `mcp` Python SDK (stdio transport)
- **LLM providers**: Anthropic (`anthropic` SDK) and OpenAI (`openai` SDK)
- **Token counting**: tiktoken (cl100k_base encoder)
- **Retry logic**: tenacity (exponential backoff)
- **Vault format**: Obsidian-compatible Markdown files

## Architecture Overview

The project is a single Python package (`codebase_wiki_builder`) that provides two entry points:

- `codewiki` CLI (Typer) — `cli.py`
- `wiki-mcp` MCP stdio server — `mcp_server.py`

Both entry points share the same core logic modules:

```
cli.py / mcp_server.py
    └─> query_engine.run_query()          # two-LLM-call pipeline
    └─> query_persistence.save_query_page()  # write queries/<slug>.md + index.md
        └─> vault.slugify()               # slug from question text
        └─> vault.wikilink()              # [[...]] link for index.md
```

Vault structure on disk:
```
<vault>/
  index.md                  # two-column table: [[wikilink]] | description
  overview.md               # top-level analysis output
  log.md                    # append-only operation log
  queries/                  # saved query pages (queries/<slug>.md)
  logs/                     # per-run debug log files
  <mirrored-codebase-path>/ # source-file summaries (*.py.md, etc.)
```

---

## Relevant Existing Code

### 1. Query Engine — `query_engine.py`

**File**: `codebase_wiki_builder/query_engine.py`

**Purpose**: Implements the two-LLM-call query workflow. Reads `index.md`, calls the LLM twice (first for relevance selection, second for answer generation), and returns a `QueryResult`.

**Key public API**:
```python
QUERY_CONTEXT_WINDOW = 128_000  # tokens

class NoRelevantFilesError(Exception): ...

@dataclass
class QueryResult:
    answer: str           # full answer text including ## Sources section
    sources: list[str]    # vault-relative paths of consulted summaries
    one_line_summary: str # LLM-generated one-liner for index.md
    stale_warnings: list[str]  # vault-relative paths of stale query pages

def run_query(
    question: str,
    vault_root: Path,
    llm_client: LLMClient,
    config: WikiConfig,
) -> QueryResult:
    ...
```

**Internal pipeline in `run_query()`**:
1. Read `index.md` (raises `FileNotFoundError` if missing)
2. Call `_collect_stale_warnings(index_content)` — scans for `⚠ stale` rows
3. First LLM call: `_build_relevance_prompt` → `llm_client.complete()` → `_parse_relevance_response()` → list of vault-relative paths
4. Raise `NoRelevantFilesError` if list is empty
5. Fill context budget via `_fill_context_budget()` (tiktoken, 128k tokens)
6. Second LLM call: `_build_answer_prompt` → `llm_client.complete()` → `_parse_answer_response()` → `(answer_text, one_line_summary)`
7. Build `## Sources` section
8. Return `QueryResult`

**Integration point for query cache pre-check**: The cache check should be inserted at the **top of `run_query()`**, after `index.md` is read (step 1) but **before** step 3 (the first LLM call). If the cache returns a hit, return the cached `QueryResult` directly and skip both LLM calls.

---

### 2. Query Persistence — `query_persistence.py`

**File**: `codebase_wiki_builder/query_persistence.py`

**Purpose**: Saves query results to `queries/<slug>.md` and registers them in `index.md`. Also provides `read_query_page()` for parsing saved pages back into a structured object.

**Key public API**:
```python
@dataclass
class QueryPage:
    path: Path
    question: str       # H1 title (original question)
    answer_body: str    # content between H1 and ## Sources
    sources: list[str]  # vault-relative paths from ## Sources
    saved_at: str       # timestamp string from ## Page Metadata
    updated_at: str
    raw_content: str

def save_query_page(
    question: str,
    result: QueryResult,
    vault_root: Path,
    log_fn: Callable[[str], None],
) -> Path:
    ...

def read_query_page(path: Path) -> QueryPage:
    ...
```

**`save_query_page()` steps**:
1. `_make_slug(question)` → calls `slugify()` → e.g. `"how-does-auth-work"`
2. `_unique_query_path(queries_dir, slug)` → finds unused `queries/<slug>.md` (or `slug-2.md` etc.)
3. Build page content: H1 + answer + `## Sources` + `## Page Metadata` (saved_at, updated_at)
4. Write file
5. Append row to `index.md`: `| [[queries/how-does-auth-work]] | <one_line_summary> |`
6. Append `query-saved` entry to `log.md`

**Page Metadata section format** (in saved file):
```
## Page Metadata
saved_at: 2026-04-29 10:00:00 UTC
updated_at: 2026-04-29 10:00:00 UTC
```

**Integration points for query cache**:
- `read_query_page()` can parse an existing saved page to retrieve the full `answer_body`, `sources`, and timestamps — all data needed to reconstruct a `QueryResult` from cache.
- To check the cache: use `slugify(question)` to build the expected path `queries/<slug>.md`, call `read_query_page()` if it exists, then check staleness.
- The slug-deduplication scheme (`slug-2.md`, etc.) means multiple questions can share a slug. A cache lookup must do exact question matching, not just slug matching.

---

### 3. CLI Query Command — `cli.py` (`_run_query_command`)

**File**: `codebase_wiki_builder/cli.py`

**Purpose**: Orchestrates the user-facing `codewiki query` command.

**`_run_query_command(question, vault_root)` flow**:
1. Setup logging, load config, build `LLMClient`
2. Call `run_query(question, vault_root, llm_client, config)` — **integration point**
3. Print stale-page warnings
4. Print the answer
5. Prompt user to save (`_prompt_save()` → default No)
6. If saved: call `save_query_page()`, print confirmation
7. Append `query` log entry

**Integration point for query cache**: The cache pre-check can be inserted either:
- *Inside `run_query()`* (transparent to both CLI and MCP — preferred), OR
- *In `_run_query_command()`* before step 2 (CLI-only, would need duplication in MCP)

The inside-`run_query()` approach is cleaner because it handles both callers uniformly.

---

### 4. MCP Server — `mcp_server.py` (`_handle_wiki_query`)

**File**: `codebase_wiki_builder/mcp_server.py`

**Purpose**: Exposes `wiki_query` MCP tool. Always auto-saves. Returns JSON with `answer`, `sources`, `saved_path`, `stale_warning`.

**`_handle_wiki_query()` flow**:
1. Validate `question` parameter
2. Call `run_query(question, vault_root, llm_client, config)` — **same integration point as CLI**
3. Call `save_query_page()` automatically (no user prompt)
4. Return JSON response

**Key difference from CLI**: MCP always saves; CLI prompts. A cache hit in `run_query()` would return the cached answer, then the MCP server would still call `save_query_page()` — which would create a duplicate file (slug dedup appends `-2.md`). The cache design must account for this: if the question already has a saved page, the MCP should detect the existing page and return it rather than saving a new copy.

---

### 5. Staleness Detection — `staleness.py`

**File**: `codebase_wiki_builder/staleness.py`

**Purpose**: After each `ingest` run, detects which saved query pages reference source summaries that have changed, and inserts a `> [!warning] Stale Content` callout banner after the H1.

**Stale page detection**:
- Builds a set of changed vault-relative paths from `ChangeSet` (new, modified, deleted source files)
- For each `queries/*.md`: reads `## Sources` section, checks if any source path is in the changed set
- If stale: inserts banner, annotates `index.md` row with `⚠ stale`

**How staleness is expressed in `index.md`**:
```
| [[queries/how-does-auth-work]] | Explains auth flow ⚠ stale |
```

**How `run_query()` reads stale warnings** (in `query_engine.py`):
```python
_STALE_ROW_RE = re.compile(r"\[\[([^\]]+)\]\].*⚠ stale")

def _collect_stale_warnings(index_content: str) -> list[str]:
    # Returns vault-relative paths (re-adds .md extension)
    # e.g. ["queries/how-does-auth-work.md"]
```

**Relevance to cache**: A query page that is marked stale should NOT be returned from cache. The cache pre-check must verify the candidate page does NOT have the stale banner (or equivalently, that its `index.md` row does not contain `⚠ stale`). The `_has_stale_banner()` function in `staleness.py` provides the check:
```python
_STALE_BANNER_RE = re.compile(r"^>\s*\[!warning\]\s*Stale Content", re.MULTILINE)

def _has_stale_banner(content: str) -> bool:
    return bool(_STALE_BANNER_RE.search(content))
```

---

### 6. `slugify()` Utility — `vault.py`

**File**: `codebase_wiki_builder/vault.py`

**Purpose**: Converts natural-language question text into a URL-safe filename slug used as the base name of saved query pages.

**Implementation**:
```python
def slugify(text: str) -> str:
    slug = text.lower()
    slug = slug.replace(" ", "-")
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    slug = slug.strip("-")
    return slug
    # May return "" if all chars are non-alphanumeric; callers handle with fallback "query"
```

**Example**: `"How does auth work?"` → `"how-does-auth-work"`

**Relevance to cache**: The slug is the primary lookup key for finding an existing query page. The cache pre-check would:
1. Compute `slug = slugify(question)` (or `"query"` if empty)
2. Check for `queries/<slug>.md`, `queries/<slug>-2.md`, etc.
3. Parse each candidate with `read_query_page()` and compare `question` (H1) for exact match (case-insensitive or exact depending on design choice)

---

### 7. LLMClient Interface — `llm_client.py`

**File**: `codebase_wiki_builder/llm_client.py`

**Purpose**: Provider-agnostic thin wrapper around Anthropic and OpenAI SDKs.

**Public interface** (what `run_query()` uses):
```python
class LLMClient:
    def __init__(self, config: WikiConfig) -> None: ...
    def complete(self, prompt: str) -> str: ...
    # Enforces inter_request_delay between calls
    # Retries on HTTP 429 with exponential backoff (up to 5 attempts)
    # Raises LLMError on non-retriable failures
```

**Relevance to cache**: The motivation for caching is entirely about skipping calls to `llm_client.complete()`. The two LLM calls in `run_query()` (relevance + answer) each go through `complete()`. A cache hit skips both. The cache pre-check sits before the first `complete()` call.

---

## index.md Format

The index is a two-column markdown table written and maintained by `index_writer.rebuild_index()` and appended to by `query_persistence.save_query_page()`:

```markdown
| File | Description |
|------|-------------|
| [[src/auth/login.py]] | Handles login and JWT validation |
| [[queries/how-does-auth-work]] | Explains auth flow end to end |
| [[queries/how-does-auth-work]] | Explains auth flow end to end ⚠ stale |
```

Key structural facts:
- Wikilinks omit `.md` extension (Obsidian convention)
- Query page rows use `queries/<slug>` as wikilink target
- `⚠ stale` suffix in the description column signals a stale page
- `_parse_existing_index()` in `index_writer.py` builds a `{wikilink_target: description}` map from this table
- The description is the LLM-generated `one_line_summary` from `QueryResult`

---

## Conventions to Follow

- **No `typer`/`rich` in logic modules** — only in `cli.py` and test helpers
- **No `typer`/`rich` in MCP module** — only `logging` and `json`
- **Deferred imports** — heavy imports (anthropic, openai, tiktoken) are deferred to function bodies in `cli.py`
- **Logging via `logging.getLogger(__name__)`** — no print statements in logic modules
- **`Callable[[str], None]` for log_fn** — passed from CLI/MCP to persistence functions for log.md writes
- **UTC timestamps** via `datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")`
- **`TYPE_CHECKING` guard** for type hints on `LLMClient`, `WikiConfig` to avoid circular imports
- **Error propagation**: `FileNotFoundError`, `NoRelevantFilesError`, and `LLMError` are raised from `run_query()` and caught by callers (CLI/MCP). The cache module should follow the same pattern.

---

## Reusable Components

- **`vault.slugify(text)`**: `vault.py` — converts question to slug; use for cache key derivation
- **`query_persistence.read_query_page(path)`**: `query_persistence.py` — parses a saved `.md` file into `QueryPage` struct; use to load candidate cache entries
- **`staleness._has_stale_banner(content)`**: `staleness.py` — detects stale callout in page content; use to invalidate cache hits
- **`query_persistence._unique_query_path()`**: `query_persistence.py` (internal) — slug dedup logic; the cache must walk the same `slug`, `slug-2`, `slug-3`... sequence to find all candidates for a given question
- **`index_writer._parse_existing_index()`**: `index_writer.py` (internal) — maps wikilink target → description (including `⚠ stale` suffix); can be used as a fast pre-filter to identify stale pages without reading each file

---

## Integration Points Summary

Where a query cache pre-check would be inserted:

| Location | File | Line context | Action |
|----------|------|--------------|--------|
| **Primary** — top of `run_query()`, after reading `index.md` and before first LLM call | `query_engine.py` | After step 2 (`stale_warnings = _collect_stale_warnings(...)`) | Check if a fresh cached page exists; if so, reconstruct and return `QueryResult` from cache, skipping steps 3–7 |
| **MCP auto-save guard** | `mcp_server.py` | After `run_query()` returns | If result came from cache and a page already exists, skip `save_query_page()` or update `updated_at` in-place instead of creating a new file |
| **CLI save prompt** | `cli.py` `_run_query_command()` | After `run_query()` returns | Optionally inform user "This answer is from the cache" before prompting to save |

The primary integration in `run_query()` handles both callers uniformly. The cache lookup needs:
1. `slugify(question)` to get the base slug
2. Scan `queries/<slug>.md`, `queries/<slug>-2.md`, ... for an exact question match
3. Parse the page with `read_query_page()` to get `answer_body`, `sources`, `one_line_summary`
4. Check `_has_stale_banner(raw_content)` — if stale, skip (cache miss)
5. Reconstruct `QueryResult(answer=..., sources=..., one_line_summary=..., stale_warnings=stale_warnings)`
   where `stale_warnings` comes from `_collect_stale_warnings(index_content)` already computed at step 2
