# Implementation Plan: MCP Server

## Spec Context

This plan implements the `wiki-mcp` entry point — an MCP stdio server that exposes exactly one tool (`wiki_query`) to AI coding agents. The module is a thin transport wrapper: it reuses `run_query()` (item 11) and `save_query_page()` (item 12) without modification, differs from the CLI only in I/O layer (structured JSON via MCP protocol instead of rich terminal output and interactive prompts), and always saves the answer automatically (no save prompt, no `save` parameter). Unknown parameters (e.g., `"save"`) are rejected with an MCP error response.

Catalog item: 15 — MCP Server
Specification section: FR-9 (all sub-requirements: FR-9.1 entry point, FR-9.2 tool schema and auto-save, FR-9.3 behavior differences, FR-9.4 protocol), Technical Context (pyproject.toml `wiki-mcp` script, `mcp` Python SDK)
Acceptance criteria addressed: AT-19 (MCP `wiki_query` always saves), AT-20 (unknown `save` parameter rejected), AT-21 (`stale_warning` is array)

## Dependencies

- **Blocked by**: Item 2 (Configuration Model) — needs `load_config()`, `WikiConfig`
- **Blocked by**: Item 11 (Query Core Logic) — calls `run_query()`; needs `QueryResult` dataclass
- **Blocked by**: Item 12 (Query Page Persistence) — calls `save_query_page()`
- **Blocks**: None (standalone transport layer; does not block any other catalog item)
- **Uses**: `mcp` Python SDK (stdio transport, `Server`, `Tool`, error types), `pathlib` (stdlib), `logging` (stdlib), `json` (stdlib), `asyncio` (stdlib); `LLMClient` from `llm_client.py` (item 3); `setup_logging()` and `append_log_md` from `logging_setup.py` (item 4)

## File Changes

### New Files

- `codebase_wiki_builder/mcp_server.py` — `main()` entry point, MCP stdio transport setup, `wiki_query` tool registration and handler, structured JSON response assembly, unknown-parameter rejection, structured exception handling

### Modified Files

- None (the `wiki-mcp` script entry point in `pyproject.toml` is already declared in item 1's plan as `wiki-mcp = "codebase_wiki_builder.mcp_server:main"`)

---

## Implementation Details

### `mcp_server.py` — Module Overview

**File**: `codebase_wiki_builder/mcp_server.py`

**Exports**:
- `main()` — synchronous entry point called by the `wiki-mcp` console script; initializes config, LLM client, logging, and starts the MCP server loop

**No exports intended for external use.** This module is a leaf in the dependency graph.

---

### MCP SDK Usage

The `mcp` Python SDK (package: `mcp`) provides:

- `mcp.server.Server` — the core server object; registers tools and handles dispatch
- `mcp.server.stdio.stdio_server()` — async context manager that wires stdin/stdout to the MCP JSON-RPC 2.0 transport
- `mcp.types.Tool` — describes a tool's name, description, and JSON schema for its input
- `mcp.types.TextContent` — wraps a string result to return from a tool handler
- `mcp.shared.exceptions.McpError` — raised inside a tool handler to return an MCP error response (as opposed to a successful result)
- `mcp.types.ErrorData` — carries the error `code` and `message` fields inside `McpError`

The SDK uses Python `asyncio`; tool handlers are `async` functions. The `main()` entry point calls `asyncio.run()` to bridge the sync script entry to the async server.

---

### Tool Schema: `wiki_query`

The tool is registered with an explicit JSON Schema for its input. The schema declares exactly one property (`question`) and marks it required. No additional properties are allowed (`"additionalProperties": false`), which causes the unknown-parameter rejection to be enforced at schema validation time.

```python
WIKI_QUERY_TOOL = mcp.types.Tool(
    name="wiki_query",
    description=(
        "Query the codebase wiki with a natural language question. "
        "Returns a grounded answer, the list of source files consulted, "
        "the path of the automatically saved query page, and any stale-page warnings. "
        "The answer is always saved to queries/ automatically."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The natural language question to answer from the wiki.",
            }
        },
        "required": ["question"],
        "additionalProperties": False,
    },
)
```

**Why `additionalProperties: false`**: Per FR-9.2 and AT-20, the `save` parameter must not be accepted. Declaring `additionalProperties: false` in the schema causes schema-validating MCP clients to reject calls with extra parameters before the handler is even invoked. For clients that do not validate at the transport layer, the handler additionally checks for unknown keys explicitly (see handler implementation below).

---

### Handler: `_handle_wiki_query()`

**Signature**:

```python
async def _handle_wiki_query(
    arguments: dict,
    vault_root: Path,
    llm_client: LLMClient,
    config: WikiConfig,
    log_fn: Callable[[str], None],
) -> list[mcp.types.TextContent]:
    """MCP tool handler for wiki_query.

    Always saves the query result automatically.
    Returns a list containing a single TextContent with a JSON-encoded response object.
    Raises McpError for all error conditions (invalid params, query failures, save failures).
    """
```

**Step-by-step logic**:

#### Step 1 — Validate parameters (reject unknown keys)

```python
known_keys = {"question"}
unknown_keys = set(arguments.keys()) - known_keys
if unknown_keys:
    raise mcp.shared.exceptions.McpError(
        mcp.types.ErrorData(
            code=mcp.types.INVALID_PARAMS,
            message=f"Unknown parameter(s): {', '.join(sorted(unknown_keys))}. "
                    f"wiki_query accepts only: question",
        )
    )
```

This explicit check catches unknown keys even when the MCP client does not enforce the JSON Schema. The `INVALID_PARAMS` error code is the JSON-RPC 2.0 standard code for invalid method parameters (-32602).

#### Step 2 — Extract and validate `question`

```python
question = arguments.get("question", "").strip()
if not question:
    raise mcp.shared.exceptions.McpError(
        mcp.types.ErrorData(
            code=mcp.types.INVALID_PARAMS,
            message="Parameter 'question' is required and must be a non-empty string.",
        )
    )
```

#### Step 3 — Call `run_query()`

```python
from codebase_wiki_builder.query_engine import NoRelevantFilesError
from codebase_wiki_builder.llm_client import LLMError

try:
    result = run_query(question, vault_root, llm_client, config)
except FileNotFoundError as exc:
    raise mcp.shared.exceptions.McpError(
        mcp.types.ErrorData(
            code=mcp.types.INTERNAL_ERROR,
            message=str(exc),
        )
    )
except NoRelevantFilesError as exc:
    raise mcp.shared.exceptions.McpError(
        mcp.types.ErrorData(
            code=mcp.types.INTERNAL_ERROR,
            message="No relevant files found for that query.",
        )
    )
except LLMError as exc:
    logger.error("LLM error in run_query: %s", exc)
    raise mcp.shared.exceptions.McpError(
        mcp.types.ErrorData(
            code=mcp.types.INTERNAL_ERROR,
            message=f"LLM error: {exc}",
        )
    )
except Exception as exc:
    logger.exception("Unexpected error in run_query: %s", exc)
    raise mcp.shared.exceptions.McpError(
        mcp.types.ErrorData(
            code=mcp.types.INTERNAL_ERROR,
            message=f"Query error: {exc}",
        )
    )
```

**Why catch specific exceptions**: `run_query()` (item 11) raises `FileNotFoundError` when `index.md` is missing, `NoRelevantFilesError` when the LLM returns no relevant files, and `LLMError` on fatal LLM failures. Catching these specific exceptions (rather than `SystemExit`) is cleaner and does not rely on `typer` being imported in `query_engine.py`. The MCP server converts each to a structured `McpError` response.

#### Step 4 — Save automatically via `save_query_page()`

```python
try:
    saved_path = save_query_page(question, result, vault_root, log_fn)
    saved_path_str = saved_path.relative_to(vault_root).as_posix()
except Exception as exc:
    logger.exception("Failed to save query page: %s", exc)
    raise mcp.shared.exceptions.McpError(
        mcp.types.ErrorData(
            code=mcp.types.INTERNAL_ERROR,
            message=f"Answer generated but failed to save: {exc}",
        )
    )
```

Save failures are reported as MCP errors because the spec guarantees `saved_path` is always present in the response. If saving fails, there is no valid `saved_path` to return, so an error response is the correct outcome.

#### Step 5 — Build and return JSON response

```python
stale_warning = result.stale_warnings if result.stale_warnings else None

response_obj = {
    "answer": result.answer,
    "sources": result.sources,
    "saved_path": saved_path_str,
    "stale_warning": stale_warning,
}

return [mcp.types.TextContent(type="text", text=json.dumps(response_obj, ensure_ascii=False))]
```

**`stale_warning` shape**: `result.stale_warnings` is `list[str]` (never `None`). The MCP response converts an empty list to `null` (`None` in Python), matching the spec's `list[str]|null` type. A non-empty list is passed through as-is (list of vault-relative file paths). This satisfies AT-21.

**`TextContent` wrapping**: MCP tool responses must be wrapped in content blocks. A single `TextContent` with `type="text"` and the JSON string as `text` is the standard way to return structured data from an MCP tool.

---

### Server Setup and Tool Registration

```python
import mcp.server
import mcp.server.stdio
import mcp.types

server = mcp.server.Server("wiki-mcp")


@server.list_tools()
async def list_tools() -> list[mcp.types.Tool]:
    return [WIKI_QUERY_TOOL]


@server.call_tool()
async def call_tool(
    name: str,
    arguments: dict,
) -> list[mcp.types.TextContent]:
    if name != "wiki_query":
        raise mcp.shared.exceptions.McpError(
            mcp.types.ErrorData(
                code=mcp.types.METHOD_NOT_FOUND,
                message=f"Unknown tool: {name}",
            )
        )
    return await _handle_wiki_query(
        arguments,
        vault_root=_vault_root,
        llm_client=_llm_client,
        config=_config,
        log_fn=_log_fn,
    )
```

The handlers reference module-level variables (`_vault_root`, `_llm_client`, `_config`, `_log_fn`) that are set by `main()` before the server loop starts. This avoids passing context through the MCP SDK's callback mechanism, which does not support extra parameters.

---

### `main()` — Entry Point

```python
import asyncio
import logging
import sys
from pathlib import Path

from codebase_wiki_builder.config import load_config
from codebase_wiki_builder.llm_client import LLMClient
from codebase_wiki_builder.logging_setup import append_log_md, setup_logging
from codebase_wiki_builder.query_engine import run_query
from codebase_wiki_builder.query_persistence import save_query_page

logger = logging.getLogger(__name__)

# Module-level state set by main() before the server loop
_vault_root: Path
_llm_client: LLMClient
_config: object
_log_fn: object


def main() -> None:
    """Entry point for the wiki-mcp console script.

    1. Resolve vault root (cwd at startup).
    2. Set up logging (debug log file; no rich terminal output).
    3. Load config.
    4. Instantiate LLMClient.
    5. Wire module-level state.
    6. Start MCP stdio server loop.
    """
    global _vault_root, _llm_client, _config, _log_fn

    vault_root = Path.cwd()
    setup_logging(vault_root)

    try:
        config = load_config(vault_root)
    except SystemExit:
        # load_config() exits with code 1 on invalid config; re-raise to terminate
        sys.exit(1)
    except Exception as exc:
        logger.critical("Failed to load config: %s", exc)
        sys.exit(1)

    llm_client = LLMClient(config)
    log_fn = lambda entry: append_log_md(vault_root, entry)

    _vault_root = vault_root
    _llm_client = llm_client
    _config = config
    _log_fn = log_fn

    asyncio.run(_serve())


async def _serve() -> None:
    """Run the MCP stdio server until the client disconnects."""
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
```

**No rich output**: `main()` does not call `rich` or `typer.echo`. Startup errors go to `logging` (which writes to the debug log file). The MCP client receives only well-formed JSON-RPC messages on stdout; any startup failure causes a clean `sys.exit(1)` before the server loop starts.

**`setup_logging(vault_root)` from item 4**: Creates `logs/YYYY-MM-DD_HH-MM-SS.log` for debug output. This is the only I/O side effect at startup. No `log.md` entry is written at server startup — only when `save_query_page()` writes a `query-saved` entry.

---

### Complete Module Skeleton

```python
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Callable

import mcp.server
import mcp.server.stdio
import mcp.shared.exceptions
import mcp.types

from codebase_wiki_builder.config import WikiConfig, load_config
from codebase_wiki_builder.llm_client import LLMClient, LLMError
from codebase_wiki_builder.logging_setup import append_log_md, setup_logging
from codebase_wiki_builder.query_engine import NoRelevantFilesError, run_query
from codebase_wiki_builder.query_persistence import save_query_page

logger = logging.getLogger(__name__)

# ── Tool definition ──────────────────────────────────────────────────────────
WIKI_QUERY_TOOL = mcp.types.Tool(...)

# ── MCP server instance ──────────────────────────────────────────────────────
server = mcp.server.Server("wiki-mcp")

# ── Module-level state (set by main() before server loop) ────────────────────
_vault_root: Path
_llm_client: LLMClient
_config: WikiConfig
_log_fn: Callable[[str], None]


# ── Tool registration ────────────────────────────────────────────────────────
@server.list_tools()
async def list_tools() -> list[mcp.types.Tool]: ...

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[mcp.types.TextContent]: ...

# ── Handler ──────────────────────────────────────────────────────────────────
async def _handle_wiki_query(
    arguments: dict,
    vault_root: Path,
    llm_client: LLMClient,
    config: WikiConfig,
    log_fn: Callable[[str], None],
) -> list[mcp.types.TextContent]: ...


# ── Entry point ──────────────────────────────────────────────────────────────
def main() -> None: ...

async def _serve() -> None: ...
```

---

## Error Handling

| Condition | Behavior |
|-----------|----------|
| Config missing or invalid at startup | `load_config()` calls `sys.exit(1)`; `main()` re-raises — server process terminates before accepting connections |
| `run_query()` raises `FileNotFoundError` (missing `index.md`) | Caught in handler; converted to `McpError(INTERNAL_ERROR, str(exc))` |
| `run_query()` raises `NoRelevantFilesError` (no relevant files) | Caught in handler; converted to `McpError(INTERNAL_ERROR, "No relevant files found...")` |
| `run_query()` raises `LLMError` (fatal LLM failure) | Caught in handler; logged at ERROR; converted to `McpError(INTERNAL_ERROR, f"LLM error: ...")` |
| `run_query()` raises any other exception | Caught in handler; logged; converted to `McpError(INTERNAL_ERROR, str(exc))` |
| `save_query_page()` raises (filesystem error, OSError) | Caught in handler; logged; converted to `McpError(INTERNAL_ERROR, "Answer generated but failed to save: ...")` |
| Unknown parameter in `arguments` (e.g., `"save"`) | Raises `McpError(INVALID_PARAMS, "Unknown parameter(s): save. wiki_query accepts only: question")` before any processing |
| `question` is absent or empty string | Raises `McpError(INVALID_PARAMS, "Parameter 'question' is required...")` |
| Unknown tool name in `call_tool` | Raises `McpError(METHOD_NOT_FOUND, "Unknown tool: <name>")` |
| Unhandled exception in `_serve()` | Propagates to `asyncio.run()`; logged at CRITICAL; process exits non-zero |

---

## Unit Test Specifications

**File**: `tests/test_mcp_server.py`

Tests use `unittest.mock` to patch `run_query` and `save_query_page`. No real network calls, no real MCP transport — handlers are tested directly as async functions using `pytest-asyncio`. The MCP server loop is not instantiated in unit tests.

---

### `_handle_wiki_query()` — happy path (AT-19)

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Returns correct JSON structure | Mock `run_query` → `QueryResult(answer="A", sources=["s.md"], one_line_summary="X", stale_warnings=[])`. Mock `save_query_page` → `Path("queries/q.md")` | Response JSON has `answer`, `sources`, `saved_path`, `stale_warning=null` | AT-19(a) |
| `saved_path` is vault-relative | `save_query_page` returns absolute path inside `vault_root` | `saved_path` in response uses vault-relative posix string (e.g., `"queries/q.md"`) | AT-19(b) |
| File saved at `saved_path` | (integration: actual file write) | File exists at the path | AT-19(b) |
| `stale_warning` is null when no stale pages | `QueryResult.stale_warnings = []` | Response JSON `stale_warning` is `null` (not `[]`) | AT-21 |
| `stale_warning` is array when stale pages exist | `QueryResult.stale_warnings = ["queries/a.md", "queries/b.md"]` | Response JSON `stale_warning` is `["queries/a.md", "queries/b.md"]` | AT-21 |
| `save_query_page` called unconditionally | Any valid question | `save_query_page` called exactly once per `wiki_query` invocation | FR-9.2: always saves |

**Key Scenario: Complete happy-path response shape (AT-19)**

```python
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_handle_wiki_query_happy_path(tmp_path):
    from codebase_wiki_builder.mcp_server import _handle_wiki_query
    from codebase_wiki_builder.query_engine import QueryResult

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "queries").mkdir()
    saved_file = vault / "queries" / "what-does-this-do.md"
    saved_file.write_text("# What does this do?\n\nAnswer.\n")

    mock_result = QueryResult(
        answer="This codebase does X.\n\n## Sources\n- src/main.py.md",
        sources=["src/main.py.md"],
        one_line_summary="Describes what the codebase does",
        stale_warnings=[],
    )

    with patch("codebase_wiki_builder.mcp_server.run_query", return_value=mock_result), \
         patch("codebase_wiki_builder.mcp_server.save_query_page", return_value=saved_file):

        contents = await _handle_wiki_query(
            arguments={"question": "What does this do?"},
            vault_root=vault,
            llm_client=MagicMock(),
            config=MagicMock(),
            log_fn=lambda e: None,
        )

    assert len(contents) == 1
    data = json.loads(contents[0].text)
    assert data["answer"] == mock_result.answer
    assert data["sources"] == ["src/main.py.md"]
    assert data["saved_path"] == "queries/what-does-this-do.md"
    assert data["stale_warning"] is None  # empty list → null
```

---

### `_handle_wiki_query()` — unknown parameter rejection (AT-20)

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| `save` parameter present | `arguments = {"question": "Q?", "save": False}` | Raises `McpError` with `INVALID_PARAMS` code | AT-20: `save` not accepted |
| `save: true` also rejected | `arguments = {"question": "Q?", "save": True}` | Same — `McpError(INVALID_PARAMS)` | No `save` parameter at all |
| Arbitrary unknown key | `arguments = {"question": "Q?", "foo": "bar"}` | Raises `McpError(INVALID_PARAMS)` | General unknown-key rejection |
| Multiple unknown keys | `arguments = {"question": "Q?", "save": False, "verbose": True}` | `McpError` message lists both `save` and `verbose` | Error message is informative |
| No unknown keys | `arguments = {"question": "Q?"}` | No error raised | Only `question` is always allowed |

**Key Scenario: `save` parameter rejected (AT-20)**

```python
@pytest.mark.asyncio
async def test_save_parameter_rejected(tmp_path):
    import mcp.shared.exceptions
    from codebase_wiki_builder.mcp_server import _handle_wiki_query

    vault = tmp_path / "vault"
    vault.mkdir()

    with pytest.raises(mcp.shared.exceptions.McpError) as exc_info:
        await _handle_wiki_query(
            arguments={"question": "What does this do?", "save": False},
            vault_root=vault,
            llm_client=MagicMock(),
            config=MagicMock(),
            log_fn=lambda e: None,
        )

    error = exc_info.value
    assert "save" in error.error.message.lower()
    # INVALID_PARAMS = -32602 per JSON-RPC 2.0
    assert error.error.code == -32602
```

---

### `_handle_wiki_query()` — empty/missing question

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| `question` is empty string | `arguments = {"question": ""}` | `McpError(INVALID_PARAMS)` | Must be non-empty |
| `question` is whitespace-only | `arguments = {"question": "   "}` | `McpError(INVALID_PARAMS)` after strip | Trimmed to empty |
| `question` key absent | `arguments = {}` | `McpError(INVALID_PARAMS)` | Required parameter |

---

### `_handle_wiki_query()` — `run_query()` failure modes

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| `run_query` raises `FileNotFoundError` (missing index) | Patch `run_query` → raises `FileNotFoundError("vault has no summaries...")` | `McpError(INTERNAL_ERROR)` with message about ingesting | Missing index → MCP error |
| `run_query` raises `NoRelevantFilesError` (no results) | Patch `run_query` → raises `NoRelevantFilesError(...)` | `McpError(INTERNAL_ERROR, "No relevant files found for that query.")` | No results → specific MCP error |
| `run_query` raises `LLMError` | Patch `run_query` → raises `LLMError("API failed")` | `McpError(INTERNAL_ERROR)` containing "LLM error" | LLM failure → MCP error |
| `run_query` raises `RuntimeError` | Patch `run_query` → raises `RuntimeError("unexpected")` | `McpError(INTERNAL_ERROR)` containing "unexpected" | Generic exception → MCP error |

---

### `_handle_wiki_query()` — `save_query_page()` failure

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Save raises `OSError` | `run_query` succeeds; `save_query_page` raises `OSError("disk full")` | `McpError(INTERNAL_ERROR, "Answer generated but failed to save: ...")` | Save failure → error (no valid `saved_path`) |

---

### `list_tools()` — tool schema correctness

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Returns exactly one tool | Call `list_tools()` | List with one element | Only `wiki_query` is exposed |
| Tool name is `wiki_query` | Call `list_tools()` | `tools[0].name == "wiki_query"` | FR-9.2: exactly one tool |
| Input schema has `question` property | Inspect `inputSchema` | `"question"` in `inputSchema["properties"]` | Tool contract |
| `additionalProperties` is false | Inspect `inputSchema` | `inputSchema["additionalProperties"] == False` | Rejects unknown params at schema level |
| `question` is required | Inspect `inputSchema` | `"question" in inputSchema["required"]` | Required field |

```python
@pytest.mark.asyncio
async def test_list_tools_returns_wiki_query():
    from codebase_wiki_builder.mcp_server import list_tools

    tools = await list_tools()
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "wiki_query"
    schema = tool.inputSchema
    assert "question" in schema["properties"]
    assert schema.get("additionalProperties") is False
    assert "question" in schema["required"]
```

---

### `call_tool()` — unknown tool name

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Known tool | `name="wiki_query"`, valid args | Delegates to `_handle_wiki_query` | Happy path |
| Unknown tool name | `name="wiki_ingest"` | `McpError(METHOD_NOT_FOUND)` | Only `wiki_query` exists |
| Empty tool name | `name=""` | `McpError(METHOD_NOT_FOUND)` | Not a recognized tool |

---

### `stale_warning` array shape (AT-21)

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| No stale pages | `stale_warnings = []` | `stale_warning` field is JSON `null` | AT-21: null not empty array |
| One stale page | `stale_warnings = ["queries/a.md"]` | `stale_warning` is `["queries/a.md"]` | AT-21: array of paths |
| Two stale pages | `stale_warnings = ["queries/a.md", "queries/b.md"]` | `stale_warning` is that exact list | AT-21 |

---

## Notes

- **Specific exception handling**: `run_query()` raises `FileNotFoundError` (missing `index.md`), `NoRelevantFilesError` (no relevant files), and `LLMError` (fatal LLM failure). The handler catches each explicitly and converts to a typed `McpError` response. A catch-all `except Exception` handles truly unexpected errors. This avoids any dependency on `typer` in `query_engine.py` and prevents `SystemExit` from accidentally terminating the server process.

- **Module-level state pattern**: The `server.list_tools()` and `server.call_tool()` decorators from the MCP SDK register callbacks at module import time. These callbacks cannot accept additional arguments beyond what the SDK provides. Storing `vault_root`, `llm_client`, `config`, and `log_fn` as module-level variables (set before the server loop starts) is the standard pattern for injecting startup context into MCP handler callbacks. This is safe because: (a) `main()` sets these before calling `asyncio.run(_serve())`, and (b) the MCP server is single-threaded (asyncio event loop) so there are no concurrency concerns.

- **No `typer` in either `mcp_server.py` or `query_engine.py`**: The MCP server does not import or use `typer`. Per the changes in item 11's plan, `query_engine.py` also does not import `typer` — it raises standard Python exceptions (`FileNotFoundError`, `NoRelevantFilesError`, `LLMError`). Both modules are free of CLI framework dependencies.

- **No rich terminal output**: `mcp_server.py` uses only `logging` for diagnostics (which goes to the debug log file). No `rich.print`, no `typer.echo`, no ANSI codes. The stdout channel is reserved for MCP JSON-RPC messages.

- **`saved_path` is always a relative posix string**: The response always contains `saved_path` as a vault-relative posix string (e.g., `"queries/what-does-this-do.md"`). This is obtained via `saved_path.relative_to(vault_root).as_posix()`. If `save_query_page()` fails, the entire response is an MCP error — there is no partial response with a null `saved_path`.

- **`stale_warning` null vs. empty array**: The spec (FR-9.2) defines `stale_warning: list[str]|null`. The response uses `null` (Python `None`) when there are no stale pages, and the list of paths when there are. The conversion is: `result.stale_warnings if result.stale_warnings else None`. This is serialized by `json.dumps()` as `null` or `["queries/a.md", ...]` respectively.

- **One `TextContent` in the return list**: MCP tool responses are `list[TextContent | ImageContent | ...]`. A single `TextContent` with `type="text"` containing the JSON string is the standard way to return structured data. Clients parse `contents[0].text` as JSON to get the response object.

- **`log_fn` construction in `main()`**: The `log_fn` lambda captures `vault_root` from the enclosing scope: `lambda entry: append_log_md(vault_root, entry)`. This is the same pattern used by the CLI query command (item 13) and lint command (items 14, 16). The `save_query_page()` function writes a `query-saved` entry via this function on each successful call.

- **Vault root is always `Path.cwd()` at startup**: Per FR-9.1, the MCP server runs from the vault root (the directory where `wiki-mcp` is launched). `main()` resolves this as `Path.cwd()` at startup, consistent with the CLI pattern.

- **`pyproject.toml` entry point already declared**: Item 1's plan declares `wiki-mcp = "codebase_wiki_builder.mcp_server:main"` in `[project.scripts]`. No changes to `pyproject.toml` are needed in this item.
