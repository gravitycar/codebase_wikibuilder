"""Unit tests for codebase_wiki_builder.mcp_server module."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import mcp.shared.exceptions
import mcp.types

from codebase_wiki_builder.mcp_server import (
    WIKI_QUERY_TOOL,
    _handle_wiki_query,
)
from codebase_wiki_builder.query_engine import NoRelevantFilesError, QueryResult
from codebase_wiki_builder.llm_client import LLMError


def make_log_fn():
    entries = []
    def log_fn(entry: str) -> None:
        entries.append(entry)
    log_fn.entries = entries
    return log_fn


def make_query_result(**kwargs):
    defaults = {
        "answer": "The auth module handles JWT tokens.\n\n## Sources\n- src/auth.py.md",
        "sources": ["src/auth.py.md"],
        "one_line_summary": "Explains JWT token handling",
        "stale_warnings": [],
    }
    defaults.update(kwargs)
    return QueryResult(**defaults)


# ---------------------------------------------------------------------------
# WIKI_QUERY_TOOL definition
# ---------------------------------------------------------------------------

class TestWikiQueryToolDefinition:
    def test_tool_name(self):
        assert WIKI_QUERY_TOOL.name == "wiki_query"

    def test_tool_has_question_parameter(self):
        props = WIKI_QUERY_TOOL.inputSchema.get("properties", {})
        assert "question" in props

    def test_question_is_required(self):
        required = WIKI_QUERY_TOOL.inputSchema.get("required", [])
        assert "question" in required


# ---------------------------------------------------------------------------
# _handle_wiki_query — parameter validation
# ---------------------------------------------------------------------------

class TestHandleWikiQueryValidation:
    def setup_method(self):
        """Common setup: vault, llm, config, log_fn fixtures."""
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.vault_root = Path(self.tmp) / "vault"
        self.vault_root.mkdir()
        (self.vault_root / "index.md").write_text("| File | Desc |\n", encoding="utf-8")
        from codebase_wiki_builder.config import WikiConfig
        self.config = WikiConfig(codebase_path=[self.tmp])
        self.llm = MagicMock()
        self.log_fn = make_log_fn()

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_rejects_empty_question(self):
        with pytest.raises(mcp.shared.exceptions.McpError) as exc_info:
            self._run(_handle_wiki_query(
                {"question": ""},
                vault_root=self.vault_root,
                llm_client=self.llm,
                config=self.config,
                log_fn=self.log_fn,
            ))
        assert exc_info.value.error.code == mcp.types.INVALID_PARAMS

    def test_rejects_whitespace_only_question(self):
        with pytest.raises(mcp.shared.exceptions.McpError) as exc_info:
            self._run(_handle_wiki_query(
                {"question": "   "},
                vault_root=self.vault_root,
                llm_client=self.llm,
                config=self.config,
                log_fn=self.log_fn,
            ))
        assert exc_info.value.error.code == mcp.types.INVALID_PARAMS

    def test_rejects_unknown_parameters(self):
        with pytest.raises(mcp.shared.exceptions.McpError) as exc_info:
            self._run(_handle_wiki_query(
                {"question": "How does auth work?", "extra_param": "value"},
                vault_root=self.vault_root,
                llm_client=self.llm,
                config=self.config,
                log_fn=self.log_fn,
            ))
        assert exc_info.value.error.code == mcp.types.INVALID_PARAMS

    def test_raises_internal_error_when_no_index(self):
        # Remove index.md to trigger FileNotFoundError
        import shutil
        vault = Path(self.tmp) / "empty_vault"
        vault.mkdir()

        with pytest.raises(mcp.shared.exceptions.McpError) as exc_info:
            self._run(_handle_wiki_query(
                {"question": "How does auth work?"},
                vault_root=vault,
                llm_client=self.llm,
                config=self.config,
                log_fn=self.log_fn,
            ))
        assert exc_info.value.error.code == mcp.types.INTERNAL_ERROR


# ---------------------------------------------------------------------------
# _handle_wiki_query — successful path
# ---------------------------------------------------------------------------

class TestHandleWikiQuerySuccess:
    def setup_method(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.vault_root = Path(self.tmp) / "vault"
        self.vault_root.mkdir()
        from codebase_wiki_builder.config import WikiConfig
        self.config = WikiConfig(codebase_path=[self.tmp])
        self.log_fn = make_log_fn()

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_returns_json_text_content(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "index.md").write_text("| File | Desc |\n", encoding="utf-8")

        query_result = make_query_result()
        from codebase_wiki_builder.config import WikiConfig
        config = WikiConfig(codebase_path=[str(tmp_path)])

        with patch("codebase_wiki_builder.mcp_server.run_query", return_value=query_result), \
             patch("codebase_wiki_builder.mcp_server.save_query_page",
                   return_value=vault / "queries" / "how-auth-works.md"):
            result = self._run(_handle_wiki_query(
                {"question": "How does auth work?"},
                vault_root=vault,
                llm_client=MagicMock(),
                config=config,
                log_fn=make_log_fn(),
            ))

        assert len(result) == 1
        assert result[0].type == "text"
        response_obj = json.loads(result[0].text)
        assert "answer" in response_obj
        assert "sources" in response_obj
        assert "saved_path" in response_obj

    def test_response_includes_stale_warnings_when_present(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        query_result = make_query_result(stale_warnings=["queries/old-query.md"])
        from codebase_wiki_builder.config import WikiConfig
        config = WikiConfig(codebase_path=[str(tmp_path)])

        with patch("codebase_wiki_builder.mcp_server.run_query", return_value=query_result), \
             patch("codebase_wiki_builder.mcp_server.save_query_page",
                   return_value=vault / "queries" / "q.md"):
            result = self._run(_handle_wiki_query(
                {"question": "What is auth?"},
                vault_root=vault,
                llm_client=MagicMock(),
                config=config,
                log_fn=make_log_fn(),
            ))

        response_obj = json.loads(result[0].text)
        assert response_obj["stale_warnings"] == ["queries/old-query.md"]

    def test_response_stale_warning_null_when_no_warnings(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        query_result = make_query_result(stale_warnings=[])
        from codebase_wiki_builder.config import WikiConfig
        config = WikiConfig(codebase_path=[str(tmp_path)])

        with patch("codebase_wiki_builder.mcp_server.run_query", return_value=query_result), \
             patch("codebase_wiki_builder.mcp_server.save_query_page",
                   return_value=vault / "queries" / "q.md"):
            result = self._run(_handle_wiki_query(
                {"question": "What is auth?"},
                vault_root=vault,
                llm_client=MagicMock(),
                config=config,
                log_fn=make_log_fn(),
            ))

        response_obj = json.loads(result[0].text)
        assert response_obj["stale_warnings"] == []


# ---------------------------------------------------------------------------
# _handle_wiki_query — error propagation
# ---------------------------------------------------------------------------

class TestHandleWikiQueryErrors:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_no_relevant_files_error_becomes_internal_error(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        from codebase_wiki_builder.config import WikiConfig
        config = WikiConfig(codebase_path=[str(tmp_path)])

        with patch("codebase_wiki_builder.mcp_server.run_query",
                   side_effect=NoRelevantFilesError("no files")):
            with pytest.raises(mcp.shared.exceptions.McpError) as exc_info:
                self._run(_handle_wiki_query(
                    {"question": "some question"},
                    vault_root=vault,
                    llm_client=MagicMock(),
                    config=config,
                    log_fn=make_log_fn(),
                ))
        assert exc_info.value.error.code == mcp.types.INTERNAL_ERROR

    def test_llm_error_becomes_internal_error(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        from codebase_wiki_builder.config import WikiConfig
        config = WikiConfig(codebase_path=[str(tmp_path)])

        with patch("codebase_wiki_builder.mcp_server.run_query",
                   side_effect=LLMError("API down")):
            with pytest.raises(mcp.shared.exceptions.McpError) as exc_info:
                self._run(_handle_wiki_query(
                    {"question": "some question"},
                    vault_root=vault,
                    llm_client=MagicMock(),
                    config=config,
                    log_fn=make_log_fn(),
                ))
        assert exc_info.value.error.code == mcp.types.INTERNAL_ERROR
