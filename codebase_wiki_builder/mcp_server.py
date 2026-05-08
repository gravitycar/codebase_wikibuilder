"""MCP stdio server for Codebase Wiki Builder.

Exposes a single MCP tool ``wiki_query`` that answers natural-language questions
from the codebase wiki and always saves the result automatically.

Entry point: ``main()`` — called by the ``wiki-mcp`` console script defined in
pyproject.toml.  The server uses the ``mcp`` Python SDK with stdio transport,
making it compatible with Claude Desktop and other MCP clients.

No ``typer`` or ``rich`` imports — this module is a pure transport layer that
writes only well-formed MCP JSON-RPC messages to stdout.  All diagnostic output
goes to the debug log file via the stdlib ``logging`` module.
"""

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

_WIKI_QUERY_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "The natural language question to answer from the wiki.",
        }
    },
    "required": ["question"],
    "additionalProperties": False,
}

_WIKI_QUERY_BASE_DESCRIPTION = (
    "Query the codebase wiki with a natural language question. "
    "Returns a grounded answer, the list of source files consulted, "
    "the path of the automatically saved query page, and any stale-page warnings. "
    "The answer is always saved to queries/ automatically."
)


def _build_tool(wiki_description: str = "") -> mcp.types.Tool:
    """Build the wiki_query Tool, optionally prefixing a codebase-specific description."""
    if wiki_description.strip():
        description = wiki_description.strip() + " " + _WIKI_QUERY_BASE_DESCRIPTION
    else:
        description = _WIKI_QUERY_BASE_DESCRIPTION
    return mcp.types.Tool(
        name="wiki_query",
        description=description,
        inputSchema=_WIKI_QUERY_TOOL_SCHEMA,
    )

# ── MCP server instance ──────────────────────────────────────────────────────

server = mcp.server.Server("wiki-mcp")

# ── Module-level state (set by main() before server loop) ────────────────────

_vault_root: Path
_llm_client: LLMClient
_config: WikiConfig
_log_fn: Callable[[str], None]


# ── Tool registration ────────────────────────────────────────────────────────


@server.list_tools()
async def list_tools() -> list[mcp.types.Tool]:
    """Return the list of tools this server exposes."""
    return [_build_tool(_config.wiki_description)]


@server.call_tool()
async def call_tool(
    name: str,
    arguments: dict,
) -> list[mcp.types.TextContent]:
    """Dispatch an incoming tool call to the appropriate handler."""
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


# ── Handler ──────────────────────────────────────────────────────────────────


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
    # Step 1 — Validate parameters: reject unknown keys
    known_keys = {"question"}
    unknown_keys = set(arguments.keys()) - known_keys
    if unknown_keys:
        raise mcp.shared.exceptions.McpError(
            mcp.types.ErrorData(
                code=mcp.types.INVALID_PARAMS,
                message=(
                    f"Unknown parameter(s): {', '.join(sorted(unknown_keys))}. "
                    "wiki_query accepts only: question"
                ),
            )
        )

    # Step 2 — Extract and validate `question`
    question = arguments.get("question", "").strip()
    if not question:
        raise mcp.shared.exceptions.McpError(
            mcp.types.ErrorData(
                code=mcp.types.INVALID_PARAMS,
                message="Parameter 'question' is required and must be a non-empty string.",
            )
        )

    # Step 3 — Call run_query()
    try:
        result = run_query(question, vault_root, llm_client, config)
    except FileNotFoundError as exc:
        raise mcp.shared.exceptions.McpError(
            mcp.types.ErrorData(
                code=mcp.types.INTERNAL_ERROR,
                message=str(exc),
            )
        ) from exc
    except NoRelevantFilesError:
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
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected error in run_query: %s", exc)
        raise mcp.shared.exceptions.McpError(
            mcp.types.ErrorData(
                code=mcp.types.INTERNAL_ERROR,
                message=f"Query error: {exc}",
            )
        ) from exc

    # Step 4 — Save automatically via save_query_page()
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
        ) from exc

    # Step 5 — Build and return JSON response
    stale_warning = result.stale_warnings if result.stale_warnings else None

    response_obj = {
        "answer": result.answer,
        "sources": result.sources,
        "saved_path": saved_path_str,
        "stale_warning": stale_warning,
    }

    return [mcp.types.TextContent(type="text", text=json.dumps(response_obj, ensure_ascii=False))]


# ── Entry point ──────────────────────────────────────────────────────────────


def main() -> None:
    """Entry point for the wiki-mcp console script.

    1. Resolve vault root (--vault arg, or cwd as fallback).
    2. Set up logging (debug log file; no rich terminal output).
    3. Load config.
    4. Instantiate LLMClient.
    5. Wire module-level state.
    6. Start MCP stdio server loop.
    """
    import argparse

    global _vault_root, _llm_client, _config, _log_fn  # noqa: PLW0603

    parser = argparse.ArgumentParser(prog="wiki-mcp", add_help=False)
    parser.add_argument(
        "--vault",
        metavar="PATH",
        default=None,
        help="Path to the Obsidian vault root (default: current directory).",
    )
    args, _ = parser.parse_known_args()

    vault_root = Path(args.vault).resolve() if args.vault else Path.cwd()
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
    log_fn: Callable[[str], None] = lambda entry: append_log_md(vault_root, entry)

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
