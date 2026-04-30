# Web Research: Codebase Wiki Builder

## Search Terms Used

- "Obsidian CLI command line interface programmatic vault management 2025/2026"
- "Obsidian CLI plugin enable disable management commands syntax 2026"
- "Obsidian CLI vault flag specify which vault command line 2026"
- "Obsidian Python library vault read write markdown files programmatic 2025"
- "Python Click vs Typer vs argparse CLI framework comparison 2025"
- "Typer Python CLI type hints async subcommands rich output 2025"
- "LLM codebase summarization tools tree-sitter chunking strategies 2025"
- "OpenAI Python SDK code summarization best practices rate limiting batching 2025"
- "anthropic Claude API vs OpenAI API code summarization Python SDK comparison 2025"
- "MD5 hash change detection incremental processing files alternatives SHA256 2025"
- "python-dotenv uv pyproject.toml modern Python project setup 2025 best practices"

---

## Key Findings

### 1. Karpathy's Wiki-Builder Pattern

**Summary**: Karpathy's gist describes an LLM-maintained persistent wiki where the LLM incrementally builds and updates structured, interlinked markdown pages from curated raw sources. Unlike traditional RAG (re-discovering knowledge from scratch each query), the wiki compounds knowledge over time — each ingest touches 10–15 relevant pages, strengthening connections and resolving contradictions.

**Key design decisions from the gist**:
- Three layers: raw sources (immutable) → wiki (LLM-generated markdown) → schema/config (CLAUDE.md-style instructions)
- Operations: `ingest` (process new sources, update pages, append log), `query` (search + synthesize + optionally file result back), `lint` (health-check for contradictions and orphan pages)
- Supporting files: `index.md` (catalog with one-line descriptions per page), `log.md` (chronological update history)
- Obsidian integration is optional/additive, not required

**How this project differs**: Instead of ingesting arbitrary documents (web pages, PDFs, podcasts), this project restricts source material to source code files and generates code-specific summaries (class/method breakdowns, cross-reference backlinks). The Karpathy pattern is the spiritual template; the implementation is specialized.

**Sources**: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f

**Recommendation**: Follow the index.md + log.md pattern closely. The three-operation model (ingest / analysis / query) maps cleanly to this project's CLI commands.

---

### 2. Obsidian CLI — CRITICAL FINDING

**Summary**: The spec's assumption about an "Obsidian CLI" at `https://obsidian.md/help/cli` is **correct** — this CLI now exists as a real, official feature. It was *not* real when the spec was originally conceived, but Obsidian shipped it as follows:

- **Early access**: Obsidian v1.12.0 (February 10, 2026) — Catalyst members only
- **General availability**: Obsidian v1.12.4 (February 27, 2026) — all users, no paid license required

The CLI operates as a "remote control" over a running Obsidian instance via IPC. If Obsidian is not running when a CLI command is issued, it will auto-launch.

**Confirmed capabilities relevant to this project**:

| Command | Description |
|---------|-------------|
| `obsidian plugins` | List installed plugins |
| `obsidian plugin:enable id=<id>` | Enable a plugin |
| `obsidian plugin:disable id=<id>` | Disable a plugin |
| `obsidian plugin:reload id=<id>` | Hot-reload a plugin (dev use) |
| `obsidian search query="..."` | Search the vault |
| `obsidian read` | Read a file |
| `obsidian create` | Create a note |
| `obsidian append` | Append content to a note |
| `obsidian files` | List files |
| `obsidian links` | Show links |
| `obsidian backlinks` | Show backlinks |
| `obsidian eval` | Execute arbitrary JavaScript with full Obsidian API access |

**Vault selection**: Use `vault="VaultName"` parameter when multiple vaults are open. Example: `obsidian search query="status::active" vault="Notes" format=json`

**Important caveat**: The CLI uses bare-word parameter syntax (`key=value`), with `--copy` being the only `--flag`-style option. All operations pass through Obsidian's internal API (requires Obsidian desktop to be running or auto-launchable). **This is not a standalone headless tool** — Obsidian must be installed and launchable on the user's system.

**The spec's plugin management syntax** (`plugins filter=core versions format=json` and `plugin:enable id=<plugin_id>`) is very close to the real CLI syntax but may not be exact. The real syntax appears to be `obsidian plugins` (not `plugins filter=core`). The architect should test the exact filter/format parameters.

**Sources**:
- https://obsidian.md/cli
- https://dev.to/shimo4228/obsidians-official-cli-is-here-no-more-hacking-your-vault-from-the-back-door-3123
- https://help.obsidian.md/cli (redirects to obsidian.md/help/cli)

**Recommendation**: The Obsidian CLI is real and usable. However, since it requires Obsidian desktop to be running, the application should:
1. Check whether Obsidian is available before trying to invoke CLI commands
2. Fall back to direct filesystem operations (writing markdown files) for core vault operations — the vault is just a directory of `.md` files and can be manipulated directly without the CLI
3. Use the CLI specifically for plugin management (enabling the search plugin) and any Obsidian-specific features
4. Document the Obsidian version requirement (≥ 1.12.4)

---

### 3. Python Libraries for Obsidian Vault Manipulation

**Summary**: Since an Obsidian vault is just a directory of markdown files, several Python libraries can read/write vault content without the CLI:

| Library | PyPI | Purpose |
|---------|------|---------|
| `obsidiantools` | `obsidiantools` | Analytics, graph analysis, backlink mapping (pandas + NetworkX) |
| `py-obsidianmd` | `py-obsidianmd` | Python interface to notes (read/write) |
| `pyobsidian` | `pyobsidian` | Find and manipulate vault notes |
| `obsidianmd-parser` | `obsidianmd-parser` | Parse Obsidian markdown with frontmatter and Dataview query support |

**Recommendation**: For this project, **direct filesystem operations** (Python's built-in `pathlib` + `os`) are sufficient and preferred for writing summary files. The vault is just a directory tree of `.md` files with `[[wikilink]]` syntax. No special library is required for the core ingest/write workflow. `obsidiantools` could be useful if graph analysis of vault connections is needed for the `analysis` command.

**Sources**: https://github.com/mfarragher/obsidiantools, https://github.com/selimrbd/py-obsidianmd

---

### 4. LLM-Based Code Summarization — Tools & Chunking Strategies

**Summary**: The 2025–2026 landscape for LLM code summarization is mature:

**Tree-sitter for AST-based chunking** is the industry standard approach:
- Tree-sitter is a parser-generator with incremental parsing; supports 66+ languages
- AST-based chunking splits code at logical boundaries (functions, classes, methods) rather than arbitrary character counts
- This outperforms naive text chunking for both recall and downstream task quality
- Tools using tree-sitter: Cursor, Aider, GitHub Copilot, Cline

**Codebase-Memory (2025)**: A recent system that constructs persistent Tree-Sitter-based knowledge graphs via MCP, with multi-phase pipelines including parallel worker pools, call-graph traversal, impact analysis, and community discovery.

**Chunking strategies for large files**:
1. **AST-aware chunking**: Split at function/class boundaries (best for code)
2. **Sliding window with overlap**: Fixed-size chunks with overlap (simpler, works for smaller files)
3. **Semantic chunking**: Use embeddings to split at semantic boundaries (overkill for this use case)

**For this project**: Since we're summarizing entire files (not retrieving chunks), chunking is only needed for very large files that exceed the LLM's context window. For most source files (< 500 lines), the entire file can be sent as-is. For large files, AST-based chunking via `tree-sitter` is the recommended approach.

**Sources**:
- https://arxiv.org/html/2603.27277v1 (Codebase-Memory paper)
- https://www.lancedb.com/blog/building-rag-on-codebases-part-1
- https://vxrl.medium.com/enhancing-llm-code-generation-with-rag-and-ast-based-chunking-5b81902ae9fc

**Recommendation**: For MVP, skip tree-sitter and send entire files to the LLM. Add a file-size guard (e.g., skip files > 100KB or truncate with a warning). Add tree-sitter support in a later iteration for large-file chunking.

---

### 5. OpenAI Python SDK vs. Anthropic SDK

**Summary**: The spec specifies the OpenAI API, but the architect should be aware of the full landscape:

**OpenAI SDK (native)**:
- Well-documented, widely used (38.7% of Python CLI projects use Click, similar ecosystem dominance for OpenAI)
- Rate limits: separate RPM (requests/minute) and TPM (tokens/minute) limits
- **Batch API**: Asynchronous, 50% lower cost, separate higher rate-limit pool, 24-hour turnaround — ideal for bulk file processing
- Context window: up to 128K tokens (GPT-4o)

**Anthropic SDK (native)**:
- Claude models support up to **200K token context window** — significant advantage for large files
- Claude Opus 4.5 leads coding benchmarks (80.9% SWE-bench)
- ~70% cheaper than GPT-4 Turbo at comparable quality
- Fewer hallucinations in factual tasks (~30% fewer per Stanford testing)
- Prompt caching supported (not available via OpenAI compatibility layer)

**Anthropic's OpenAI compatibility layer**:
- You can use the OpenAI Python SDK pointed at `https://api.anthropic.com/v1/` with a Claude API key
- Set `base_url="https://api.anthropic.com/v1/"` and `api_key=ANTHROPIC_API_KEY`
- **Not recommended for production**: Prompt caching, structured outputs, and extended thinking are not available through the compatibility layer
- Intended for evaluation/testing only

**Recommendation**: Since this is a greenfield Python project, **use the native `anthropic` SDK** instead of — or in addition to — the `openai` SDK. The 200K context window is a material advantage for large code files. Design the LLM client as an abstraction layer (a simple class with a `summarize(content)` method) that can be backed by either provider via config. The `.env` file should support both `OPENAI_API_KEY` and `ANTHROPIC_API_KEY`. Default to Anthropic for new installations but allow OpenAI as an option.

**Sources**:
- https://platform.claude.com/docs/en/api/openai-sdk
- https://collabnix.com/claude-api-vs-openai-api-2025-complete-developer-comparison-with-benchmarks-code-examples/
- https://cookbook.openai.com/examples/how_to_handle_rate_limits
- https://platform.openai.com/docs/guides/batch

---

### 6. OpenAI / LLM API Rate Limiting Best Practices

**Summary**:

- **Exponential backoff with jitter**: On 429 (rate limit) errors, wait 1s → 2s → 4s → up to ~30s max with random jitter
- **Strategic delays**: If rate limit is 20 RPM, add a 3–6 second delay between requests
- **Batch if possible**: Multiple prompts in one request reduces RPM consumption (but check max token limits per call)
- **OpenAI Batch API**: For bulk ingest operations, the Batch API offers 50% cost reduction with a separate rate limit pool and 24-hour turnaround. This is ideal for initial full-codebase ingests

**For this project**: During initial ingest of a large codebase (potentially hundreds of files), rate limiting will be a real concern. Implement:
1. Configurable delay between API calls (default 1–2s)
2. Automatic retry with exponential backoff on 429 errors
3. Optional batch mode using the OpenAI Batch API (or Anthropic's equivalent) for cost efficiency on large codebases

**Sources**:
- https://cookbook.openai.com/examples/how_to_handle_rate_limits
- https://platform.openai.com/docs/guides/batch

---

### 7. Python CLI Framework: Click vs. Typer vs. argparse

**Summary**:

| Framework | Adoption | Approach | Best For |
|-----------|----------|----------|----------|
| `argparse` | Universal (stdlib) | Verbose, manual | Zero-dependency scripts; legacy |
| `click` | 38.7% of CLI projects (2025) | Decorator-based | Production tools, mature ecosystem |
| `typer` | Growing fast | Type-hint-based, wraps click | Modern Python (3.9+), minimal boilerplate |

**Typer key features**:
- Builds on Click under the hood — all Click capabilities available
- Subcommands (`app.add_typer()`) map naturally to `ingest`, `analysis`, `query`
- Python type hints define all parameters automatically
- Built-in `rich` integration for styled terminal output
- Auto-completion out of the box
- Up to 30% faster execution than raw Click in some benchmarks (v0.9.0+)
- Async support

**Typer example for this project's structure**:
```python
import typer
app = typer.Typer()

@app.command()
def ingest(codebase: Path, vault: Path = typer.Option(None)):
    """Scan codebase and generate wiki summaries."""
    ...

@app.command()
def analysis():
    """Analyze wiki and produce overview.md."""
    ...

@app.command()
def query(question: str):
    """Query the wiki with a natural language question."""
    ...
```

**Recommendation**: Use **Typer** for this project. It is the most ergonomic choice for a new Python CLI tool with multiple subcommands, requires minimal boilerplate, and integrates naturally with type hints and `rich` for output formatting. It wraps Click, so the team has access to Click's full ecosystem if needed.

**Sources**:
- https://typer.tiangolo.com/
- https://github.com/fastapi/typer
- https://dasroot.net/posts/2025/12/building-cli-tools-python-click-typer-argparse/

---

### 8. MD5 for Change Detection

**Summary**:

**MD5 is acceptable for this use case**. The security concerns around MD5 (collision attacks) are irrelevant here because:
- We're not using it for security/cryptographic purposes
- We're fingerprinting our own trusted source files to detect whether they've changed
- There is no adversary attempting to craft collisions

**Performance**: MD5 is slightly faster than SHA-256 and produces shorter digests (32 hex chars vs. 64). For a simple change-detection cache embedded in a markdown file, MD5 is a fine choice.

**Alternatives worth considering**:
- **SHA-256**: Drop-in alternative with `hashlib.sha256()`. Only ~2x slower than MD5. Recommended if there's any concern about future-proofing or if the hash will be used for integrity verification.
- **BLAKE2**: High-performance cryptographic hash, often faster than SHA-256, available in Python's `hashlib` as `hashlib.blake2b()`. Best pure-performance option if MD5 feels insufficient.

**Implementation note**: For large files, read and hash in 4096-byte chunks to avoid loading entire large files into memory:
```python
import hashlib

def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            h.update(chunk)
    return h.hexdigest()
```

**Recommendation**: Stick with MD5 as specified. It's correct for this use case. Add a note in code comments explaining why MD5 is acceptable (non-security use case, change detection only).

**Sources**:
- https://freetoolonline.com/guides/md5-vs-sha256-when-to-hash.html
- https://www.mindstick.com/interview/34133/how-do-you-generate-a-file-hash-sha256-md5-to-detect-changes

---

### 9. Modern Python Project Setup (uv + pyproject.toml)

**Summary**: The ecosystem standard for new Python projects in 2025–2026 is:

- **`uv`** as the package manager (Rust-powered, 10–100x faster than pip)
- **`pyproject.toml`** as the single project configuration file
- **`uv.lock`** for reproducible environments

**uv key commands**:
```bash
uv init codebase-wiki-builder    # create project
uv add typer anthropic openai python-dotenv  # add dependencies
uv run python -m codebase_wiki_builder ingest  # run with env
uv run --env-file .env ...       # load .env automatically
```

**uv + dotenv**: `uv run` can load `.env` files natively via `--env-file .env` flag, reducing (but not eliminating) the need for `python-dotenv` at runtime.

**Recommendation**: Use `uv` + `pyproject.toml` for this project. Include a `[project.scripts]` entry to make the CLI installable as a global command (e.g., `wiki = "codebase_wiki_builder.cli:app"`).

**Sources**:
- https://docs.astral.sh/uv/guides/projects/
- https://medium.com/@philip.mutua/python-uv-pyproject-toml-the-fastest-way-to-run-python-apps-913d6213c111

---

## Recommended Approaches

1. **CLI framework**: Typer (wraps Click, type-hint-based, minimal boilerplate, rich integration)
2. **Project packaging**: `uv` + `pyproject.toml` (modern Python standard)
3. **LLM provider**: Abstract the LLM client; default to Anthropic SDK (200K context, better coding benchmarks, lower cost), with OpenAI as a configurable alternative
4. **Vault manipulation**: Direct filesystem operations (`pathlib`) for core write operations; invoke Obsidian CLI only for plugin management and Obsidian-specific features
5. **Obsidian CLI**: It is real as of Obsidian v1.12.4 (Feb 2026). Use it for plugin enable/disable. Require Obsidian to be installed; the CLI auto-launches it if not running
6. **Change detection**: MD5 is fine (non-security use case). Implement chunked file reading for memory efficiency
7. **Rate limiting**: Exponential backoff + configurable delay + optional Batch API for large codebases
8. **Code chunking**: Skip tree-sitter for MVP; add a file-size guard to skip/truncate files above a threshold

---

## Potential Pitfalls

1. **Obsidian CLI requires Obsidian desktop**: The CLI is not a headless standalone tool. It communicates with a running Obsidian instance via IPC. If the user does not have Obsidian installed (or the right version), plugin management will fail. The application must gracefully degrade — writing markdown files to the vault directory always works without the CLI.

2. **Obsidian CLI vault specification**: When the user has multiple vaults open, the `vault="VaultName"` parameter is needed. If running from the vault root (as the spec proposes), this might need to be inferred from the directory name. Test this carefully.

3. **LLM API rate limits during bulk ingest**: Processing 100+ files in sequence will hit rate limits. Without delay/backoff logic, the first ingest of a large codebase will likely fail partway through. Implement from day one.

4. **Context window limits for large files**: Very large files (e.g., minified JS, generated code, large config files) may exceed even Claude's 200K token limit. The spec does not mention handling this. Add a pre-check: compute approximate token count before sending (rough rule: 1 token ≈ 4 characters for code).

5. **Binary files**: The spec does not mention binary files (images, compiled outputs, etc.). The scanner should skip non-text files. Use a MIME-type check or extension whitelist/blacklist.

6. **Deleted files with backlinks**: The spec mentions cleaning up backlinks when a source file is deleted. This requires reading every summary file that contains a backlink to the deleted file and updating it — a potentially expensive O(n) scan on every deletion. Consider an inverted index (e.g., in `index.md` or a separate `_links.json`) to make this efficient.

7. **First-run interactive prompting**: The spec proposes asking the user for the target codebase path on first run. Typer supports `typer.prompt()` for this, or an `init` subcommand is cleaner UX. Consider an explicit `wiki init` command rather than implicit prompting during `ingest`.

8. **MD5 collision false negatives**: Astronomically unlikely for this use case, but worth noting. If a file changes to a content that happens to hash identically (MD5 collision), the summary won't be updated. Acceptable risk for a local dev tool.

---

## Libraries/Services to Consider

| Library/Tool | Purpose | Recommendation |
|---|---|---|
| `typer` | CLI framework | **Use** — primary CLI framework |
| `anthropic` | Anthropic Python SDK | **Use** — primary LLM client |
| `openai` | OpenAI Python SDK | **Include** as optional/alternative backend |
| `python-dotenv` | Load `.env` at runtime | **Use** — standard for secrets management |
| `pathlib` (stdlib) | Filesystem operations | **Use** — vault file manipulation |
| `hashlib` (stdlib) | MD5/SHA hashing | **Use** — change detection |
| `rich` | Terminal output formatting | **Use** — bundled with Typer, use for progress bars and status |
| `uv` | Package manager | **Use** — project setup and dependency management |
| `ruff` | Linter/formatter | **Use** — already in .gitignore |
| `mypy` | Type checker | **Use** — already in .gitignore |
| `pytest` | Test runner | **Use** — already in .gitignore |
| `obsidiantools` | Vault graph analysis | **Consider** for `analysis` command |
| `tree-sitter` | AST-based code chunking | **Defer** — useful for large file handling in v2 |
| `tenacity` | Retry logic with backoff | **Consider** — clean abstraction for API retry logic |
