"""Integration tests for the query cache (check_query_cache / run_query cache path).

Tests end-to-end flows using real filesystem (tmp_path) and mocked LLMClient.complete().

Covers:
  1. Stage 1 cache hit end-to-end (AT-1): slug match → no LLM calls, from_cache=True.
  2. Stage 1 stale page = miss (AT-4): stale banner → LLM IS called, from_cache=False.
  3. Stage 2 cache hit end-to-end (AT-5): no slug match, LLM pre-check returns path.
  4. Full miss end-to-end (AT-6): LLM returns NO_MATCH → full pipeline runs, from_cache=False.
  5. AT-13 stale_warnings always propagated: stale summary pages appear in
     result.stale_warnings regardless of cache vs. fresh path.
  6. AT-7 Stage 2 skipped when no query pages in index.md.
  7. AT-8 MCP-style: cache hit prevents duplicate file creation (from_cache=True).
  8. AT-10 from_cache defaults to False on QueryResult.
  9. AT-11 read_query_page exception → full pipeline (resilience).
  10. AT-12 Stage 2 LLM error resilience → full pipeline runs.
  11. AT-15 SEC-3 prefix check: path not starting with queries/ → cache miss.
  12. AT-16 SEC-3 containment check: traversal path → cache miss.
  13. AT-17 SEC-3 allowlist check: unlisted path → cache miss.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_wiki_builder.config import WikiConfig
from codebase_wiki_builder.query_engine import run_query, QueryResult

logger = logging.getLogger("test_integration_cache")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def make_config(codebase_path: Path) -> WikiConfig:
    return WikiConfig(
        codebase_path=[str(codebase_path)],
        llm_provider="anthropic",
        llm_model="claude-sonnet-4-6",
        file_size_threshold=100_000,
        inter_request_delay=0.0,
    )


def _write_query_page(
    path: Path,
    question: str = "How does auth work?",
    answer_body: str = "Auth uses JWT tokens.",
    sources: list[str] | None = None,
    saved_at: str = "2026-04-29 10:00:00 UTC",
    stale: bool = False,
) -> str:
    """Write a minimal valid query page to path. Returns the file content."""
    if sources is None:
        sources = ["src/auth.py.md"]
    stale_banner = "> [!warning] Stale Content\n> Sources changed.\n\n" if stale else ""
    source_lines = "\n".join(f"- {s}" for s in sources)
    content = (
        f"# {question}\n\n"
        f"{stale_banner}"
        f"{answer_body}\n\n"
        f"## Sources\n"
        f"{source_lines}\n\n"
        f"## Page Metadata\n"
        f"saved_at: {saved_at}\n"
        f"updated_at: {saved_at}\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return content


def _make_vault_with_index(
    tmp_path: Path,
    index_rows: list[str] | None = None,
) -> tuple[Path, Path]:
    """Create a minimal vault with index.md and a src/ summary file.

    Returns (vault_root, codebase_root).
    """
    vault = tmp_path / "vault"
    codebase = tmp_path / "codebase"
    vault.mkdir()
    codebase.mkdir()

    src_dir = vault / "src"
    src_dir.mkdir()
    (src_dir / "auth.py.md").write_text(
        "# src/auth.py\n\nHandles JWT authentication.\n\n"
        "## References\n\n<!-- md5: aabbccdd00112233 -->\n",
        encoding="utf-8",
    )

    # Build index.md
    header = "| File | Description |\n|------|-------------|\n"
    rows = index_rows if index_rows is not None else ["| [[src/auth.py]] | Auth module |\n"]
    (vault / "index.md").write_text(header + "".join(rows), encoding="utf-8")

    return vault, codebase


# ---------------------------------------------------------------------------
# Test 1: Stage 1 cache hit end-to-end (AT-1)
# ---------------------------------------------------------------------------

class TestStage1CacheHitEndToEnd:
    """Stage 1 slug match → no LLM calls, from_cache=True, answer contains cached content."""

    def test_stage1_hit_no_llm_calls(self, tmp_path, monkeypatch):
        """AT-1: Exact slug match returns cached result without any LLM call."""
        vault, codebase = _make_vault_with_index(
            tmp_path,
            index_rows=[
                "| [[src/auth.py]] | Auth module |\n",
                "| [[queries/how-does-auth-work]] | Explains auth |\n",
            ],
        )
        queries_dir = vault / "queries"
        queries_dir.mkdir()
        _write_query_page(
            queries_dir / "how-does-auth-work.md",
            question="How does auth work?",
            answer_body="Auth uses JWT tokens via middleware.",
            saved_at="2026-04-29 10:00:00 UTC",
        )
        config = make_config(codebase)

        monkeypatch.setattr("codebase_wiki_builder.query_engine._count_tokens", lambda t: 100)
        mock_llm = MagicMock()

        result = run_query("How does auth work?", vault, mock_llm, config)

        # No LLM calls should have been made
        assert mock_llm.complete.call_count == 0, (
            f"Expected 0 LLM calls but got {mock_llm.complete.call_count}"
        )
        assert result.from_cache is True
        assert "Auth uses JWT tokens via middleware." in result.answer

    def test_stage1_hit_cached_path_and_cached_at(self, tmp_path, monkeypatch):
        """Stage 1 hit populates cached_path and cached_at on QueryResult."""
        vault, codebase = _make_vault_with_index(
            tmp_path,
            index_rows=[
                "| [[src/auth.py]] | Auth module |\n",
                "| [[queries/how-does-auth-work]] | Explains auth |\n",
            ],
        )
        queries_dir = vault / "queries"
        queries_dir.mkdir()
        _write_query_page(
            queries_dir / "how-does-auth-work.md",
            question="How does auth work?",
            saved_at="2026-04-29 10:00:00 UTC",
        )
        config = make_config(codebase)

        monkeypatch.setattr("codebase_wiki_builder.query_engine._count_tokens", lambda t: 100)
        mock_llm = MagicMock()

        result = run_query("How does auth work?", vault, mock_llm, config)

        assert result.from_cache is True
        assert result.cached_path == Path("queries/how-does-auth-work.md")
        assert result.cached_at == "2026-04-29 10:00:00 UTC"

    def test_stage1_hit_case_insensitive(self, tmp_path, monkeypatch):
        """AT-2: Stage 1 matches correctly regardless of input case (normalization)."""
        vault, codebase = _make_vault_with_index(
            tmp_path,
            index_rows=[
                "| [[src/auth.py]] | Auth module |\n",
                "| [[queries/how-does-auth-work]] | Explains auth |\n",
            ],
        )
        queries_dir = vault / "queries"
        queries_dir.mkdir()
        _write_query_page(
            queries_dir / "how-does-auth-work.md",
            question="How does auth work?",
        )
        config = make_config(codebase)

        monkeypatch.setattr("codebase_wiki_builder.query_engine._count_tokens", lambda t: 100)
        mock_llm = MagicMock()

        result = run_query("HOW DOES AUTH WORK?", vault, mock_llm, config)

        assert result.from_cache is True
        assert mock_llm.complete.call_count == 0

    def test_stage1_hit_numeric_suffix(self, tmp_path, monkeypatch):
        """AT-3: Stage 1 walks -2 suffix and returns it when first file has different question."""
        vault, codebase = _make_vault_with_index(
            tmp_path,
            index_rows=[
                "| [[src/auth.py]] | Auth module |\n",
                "| [[queries/how-does-auth-work]] | Some other question |\n",
                "| [[queries/how-does-auth-work-2]] | Explains auth |\n",
            ],
        )
        queries_dir = vault / "queries"
        queries_dir.mkdir()
        # First file has a different question
        _write_query_page(
            queries_dir / "how-does-auth-work.md",
            question="What is authentication exactly?",
            answer_body="This is a different answer.",
        )
        # Second file has the matching question
        _write_query_page(
            queries_dir / "how-does-auth-work-2.md",
            question="How does auth work?",
            answer_body="Auth uses JWT tokens via middleware.",
            saved_at="2026-04-30 12:00:00 UTC",
        )
        config = make_config(codebase)

        monkeypatch.setattr("codebase_wiki_builder.query_engine._count_tokens", lambda t: 100)
        mock_llm = MagicMock()

        result = run_query("How does auth work?", vault, mock_llm, config)

        assert result.from_cache is True
        assert mock_llm.complete.call_count == 0
        assert "Auth uses JWT tokens via middleware." in result.answer
        assert result.cached_path == Path("queries/how-does-auth-work-2.md")


# ---------------------------------------------------------------------------
# Test 2: Stage 1 stale page = miss (AT-4)
# ---------------------------------------------------------------------------

class TestStage1StalePageMiss:
    """Stale page → cache miss → LLM pipeline runs."""

    def test_stale_page_triggers_full_pipeline(self, tmp_path, monkeypatch):
        """AT-4: Stale query page causes cache miss; full two-LLM-call pipeline runs."""
        vault, codebase = _make_vault_with_index(
            tmp_path,
            index_rows=[
                "| [[src/auth.py]] | Auth module |\n",
                "| [[queries/how-does-auth-work]] | Explains auth ⚠ stale |\n",
            ],
        )
        queries_dir = vault / "queries"
        queries_dir.mkdir()
        _write_query_page(
            queries_dir / "how-does-auth-work.md",
            question="How does auth work?",
            stale=True,
        )
        config = make_config(codebase)

        monkeypatch.setattr("codebase_wiki_builder.query_engine._count_tokens", lambda t: 100)
        mock_llm = MagicMock()
        mock_llm.complete.side_effect = [
            # Stage 2 is also run (stale page is still in index), but there's only
            # one candidate which is stale → Stage 2 also misses.
            # Actually the stale page fails to produce a non-stale candidate in Stage 2.
            # Stage 2 prompt is built, LLM may return path, but page is stale → miss.
            # Let's have Stage 2 return NO_MATCH to go directly to full pipeline.
            "NO_MATCH",
            # Full pipeline: relevance call
            '["src/auth.py.md"]',
            # Full pipeline: answer call
            json.dumps({
                "answer": "Fresh answer about auth.",
                "one_line_summary": "Fresh explanation of auth",
            }),
        ]

        result = run_query("How does auth work?", vault, mock_llm, config)

        # LLM must have been called (Stage 2 + full pipeline = 3 calls)
        assert mock_llm.complete.call_count == 3
        assert result.from_cache is False
        assert "Fresh answer about auth." in result.answer

    def test_stale_page_from_cache_is_false(self, tmp_path, monkeypatch):
        """Stale cache match returns from_cache=False on the final result."""
        vault, codebase = _make_vault_with_index(
            tmp_path,
            index_rows=[
                "| [[src/auth.py]] | Auth module |\n",
                "| [[queries/how-does-auth-work]] | Explains auth ⚠ stale |\n",
            ],
        )
        queries_dir = vault / "queries"
        queries_dir.mkdir()
        _write_query_page(
            queries_dir / "how-does-auth-work.md",
            question="How does auth work?",
            stale=True,
        )
        config = make_config(codebase)

        monkeypatch.setattr("codebase_wiki_builder.query_engine._count_tokens", lambda t: 100)
        mock_llm = MagicMock()
        mock_llm.complete.side_effect = [
            "NO_MATCH",
            '["src/auth.py.md"]',
            json.dumps({
                "answer": "Fresh answer.",
                "one_line_summary": "Fresh.",
            }),
        ]

        result = run_query("How does auth work?", vault, mock_llm, config)
        assert result.from_cache is False


# ---------------------------------------------------------------------------
# Test 3: Stage 2 cache hit end-to-end (AT-5)
# ---------------------------------------------------------------------------

class TestStage2CacheHitEndToEnd:
    """No slug match, but Stage 2 LLM pre-check finds a match."""

    def test_stage2_hit_returns_cached_result(self, tmp_path, monkeypatch):
        """AT-5: Stage 2 returns match → from_cache=True, complete() called once."""
        vault, codebase = _make_vault_with_index(
            tmp_path,
            index_rows=[
                "| [[src/auth.py]] | Auth module |\n",
                "| [[queries/how-does-auth-work]] | Explains auth |\n",
            ],
        )
        queries_dir = vault / "queries"
        queries_dir.mkdir()
        _write_query_page(
            queries_dir / "how-does-auth-work.md",
            question="How does auth work?",
            answer_body="Auth uses JWT tokens via middleware.",
            saved_at="2026-04-29 10:00:00 UTC",
        )
        config = make_config(codebase)

        monkeypatch.setattr("codebase_wiki_builder.query_engine._count_tokens", lambda t: 100)
        mock_llm = MagicMock()
        # Stage 2 pre-check returns the matching path
        mock_llm.complete.return_value = "queries/how-does-auth-work"

        # Ask with different phrasing — slug will NOT match "how-does-auth-work"
        result = run_query("Explain how authentication is handled", vault, mock_llm, config)

        # Only ONE LLM call (Stage 2 pre-check only)
        assert mock_llm.complete.call_count == 1
        assert result.from_cache is True
        assert "Auth uses JWT tokens via middleware." in result.answer

    def test_stage2_hit_cached_at_populated(self, tmp_path, monkeypatch):
        """Stage 2 hit populates cached_at from the matched page's saved_at."""
        vault, codebase = _make_vault_with_index(
            tmp_path,
            index_rows=[
                "| [[src/auth.py]] | Auth module |\n",
                "| [[queries/how-does-auth-work]] | Explains auth |\n",
            ],
        )
        queries_dir = vault / "queries"
        queries_dir.mkdir()
        _write_query_page(
            queries_dir / "how-does-auth-work.md",
            question="How does auth work?",
            saved_at="2026-04-29 10:00:00 UTC",
        )
        config = make_config(codebase)

        monkeypatch.setattr("codebase_wiki_builder.query_engine._count_tokens", lambda t: 100)
        mock_llm = MagicMock()
        mock_llm.complete.return_value = "queries/how-does-auth-work"

        result = run_query("Explain how authentication is handled", vault, mock_llm, config)

        assert result.from_cache is True
        assert result.cached_at == "2026-04-29 10:00:00 UTC"
        assert result.cached_path == Path("queries/how-does-auth-work.md")


# ---------------------------------------------------------------------------
# Test 4: Full miss end-to-end (AT-6)
# ---------------------------------------------------------------------------

class TestFullMissEndToEnd:
    """Both stages miss → full two-LLM-call pipeline runs."""

    def test_full_miss_runs_full_pipeline(self, tmp_path, monkeypatch):
        """AT-6: Stage 2 NO_MATCH → complete() called 3 times total; from_cache=False."""
        vault, codebase = _make_vault_with_index(
            tmp_path,
            index_rows=[
                "| [[src/auth.py]] | Auth module |\n",
                "| [[queries/how-does-auth-work]] | Explains auth |\n",
            ],
        )
        queries_dir = vault / "queries"
        queries_dir.mkdir()
        _write_query_page(
            queries_dir / "how-does-auth-work.md",
            question="How does auth work?",
        )
        config = make_config(codebase)

        monkeypatch.setattr("codebase_wiki_builder.query_engine._count_tokens", lambda t: 100)
        mock_llm = MagicMock()
        mock_llm.complete.side_effect = [
            # Stage 2 pre-check returns NO_MATCH
            "NO_MATCH",
            # Full pipeline: relevance call
            '["src/auth.py.md"]',
            # Full pipeline: answer call
            json.dumps({
                "answer": "Fresh detailed answer about auth.",
                "one_line_summary": "Full pipeline answer",
            }),
        ]

        result = run_query("What is the authorization flow?", vault, mock_llm, config)

        assert mock_llm.complete.call_count == 3
        assert result.from_cache is False
        assert "Fresh detailed answer about auth." in result.answer

    def test_full_miss_answer_from_llm(self, tmp_path, monkeypatch):
        """Full miss result comes from LLM pipeline, not cache."""
        vault, codebase = _make_vault_with_index(tmp_path)
        config = make_config(codebase)

        monkeypatch.setattr("codebase_wiki_builder.query_engine._count_tokens", lambda t: 100)
        mock_llm = MagicMock()
        mock_llm.complete.side_effect = [
            # Stage 2: no query rows → Stage 2 skipped → directly to full pipeline
            '["src/auth.py.md"]',
            json.dumps({
                "answer": "LLM-generated answer.",
                "one_line_summary": "Generated fresh.",
            }),
        ]

        # Vault has no queries/ dir → Stage 1 and Stage 2 both miss immediately
        result = run_query("How does auth work?", vault, mock_llm, config)

        assert result.from_cache is False
        assert "LLM-generated answer." in result.answer
        # Only 2 LLM calls (full pipeline, no Stage 2 because no query rows)
        assert mock_llm.complete.call_count == 2


# ---------------------------------------------------------------------------
# Test 5: AT-13 stale_warnings always propagated
# ---------------------------------------------------------------------------

class TestStaleWarningsPropagation:
    """Stale summary pages in index.md appear in result.stale_warnings for both
    cache-hit and fresh-result paths."""

    def test_stale_warnings_on_cache_hit(self, tmp_path, monkeypatch):
        """AT-13 (cache hit path): stale_warnings includes other stale pages."""
        vault, codebase = _make_vault_with_index(
            tmp_path,
            index_rows=[
                "| [[src/auth.py]] | Auth module |\n",
                "| [[queries/how-does-auth-work]] | Explains auth |\n",
                "| [[queries/old-query]] | Old answer ⚠ stale |\n",
            ],
        )
        queries_dir = vault / "queries"
        queries_dir.mkdir()
        # The query we're asking about — non-stale, will be a cache hit
        _write_query_page(
            queries_dir / "how-does-auth-work.md",
            question="How does auth work?",
            answer_body="Auth uses JWT tokens.",
        )
        # A stale query page (different question) — should appear in stale_warnings
        _write_query_page(
            queries_dir / "old-query.md",
            question="What is the old query?",
            stale=True,
        )
        config = make_config(codebase)

        monkeypatch.setattr("codebase_wiki_builder.query_engine._count_tokens", lambda t: 100)
        mock_llm = MagicMock()

        result = run_query("How does auth work?", vault, mock_llm, config)

        # Should be a cache hit
        assert result.from_cache is True
        assert mock_llm.complete.call_count == 0
        # Stale warnings for the OTHER stale page must still appear
        assert "queries/old-query.md" in result.stale_warnings

    def test_stale_warnings_on_fresh_result(self, tmp_path, monkeypatch):
        """AT-13 (fresh result path): stale_warnings still collected even without cache hit."""
        vault, codebase = _make_vault_with_index(
            tmp_path,
            index_rows=[
                "| [[src/auth.py]] | Auth module |\n",
                "| [[queries/old-answer]] | Old question ⚠ stale |\n",
            ],
        )
        queries_dir = vault / "queries"
        queries_dir.mkdir()
        _write_query_page(
            queries_dir / "old-answer.md",
            question="What is the old answer?",
            stale=True,
        )
        config = make_config(codebase)

        monkeypatch.setattr("codebase_wiki_builder.query_engine._count_tokens", lambda t: 100)
        mock_llm = MagicMock()
        mock_llm.complete.side_effect = [
            # Stage 2: LLM returns NO_MATCH
            "NO_MATCH",
            # Full pipeline
            '["src/auth.py.md"]',
            json.dumps({
                "answer": "Fresh auth answer.",
                "one_line_summary": "Auth explained.",
            }),
        ]

        result = run_query("How does auth work?", vault, mock_llm, config)

        assert result.from_cache is False
        assert "queries/old-answer.md" in result.stale_warnings


# ---------------------------------------------------------------------------
# Test 6: AT-7 Stage 2 skipped when no query pages exist
# ---------------------------------------------------------------------------

class TestStage2SkippedNoQueryPages:
    """When index.md has no query page rows, Stage 2 LLM call is skipped."""

    def test_no_query_rows_means_no_stage2_llm_call(self, tmp_path, monkeypatch):
        """AT-7: Without query rows in index.md, only 2 LLM calls (full pipeline)."""
        # Vault has no queries/ directory and no query rows in index.md
        vault, codebase = _make_vault_with_index(tmp_path)
        config = make_config(codebase)

        monkeypatch.setattr("codebase_wiki_builder.query_engine._count_tokens", lambda t: 100)
        mock_llm = MagicMock()
        mock_llm.complete.side_effect = [
            '["src/auth.py.md"]',
            json.dumps({
                "answer": "Auth explained.",
                "one_line_summary": "Explains auth.",
            }),
        ]

        result = run_query("How does auth work?", vault, mock_llm, config)

        # Exactly 2 calls — Stage 2 was skipped entirely (no query rows)
        assert mock_llm.complete.call_count == 2
        assert result.from_cache is False


# ---------------------------------------------------------------------------
# Test 7: AT-8 cache hit suppresses duplicate file creation
# ---------------------------------------------------------------------------

class TestCacheHitPreventsDuplicateFile:
    """AT-8: When from_cache=True, save_query_page() is NOT called → no duplicate file."""

    def test_cache_hit_no_new_file(self, tmp_path, monkeypatch):
        """AT-8: run_query() returns from_cache=True; caller should not save → no -2 file."""
        vault, codebase = _make_vault_with_index(
            tmp_path,
            index_rows=[
                "| [[src/auth.py]] | Auth module |\n",
                "| [[queries/how-does-auth-work]] | Explains auth |\n",
            ],
        )
        queries_dir = vault / "queries"
        queries_dir.mkdir()
        _write_query_page(
            queries_dir / "how-does-auth-work.md",
            question="How does auth work?",
        )
        config = make_config(codebase)

        monkeypatch.setattr("codebase_wiki_builder.query_engine._count_tokens", lambda t: 100)
        mock_llm = MagicMock()

        result = run_query("How does auth work?", vault, mock_llm, config)

        assert result.from_cache is True
        # The signal for MCP/CLI to skip save_query_page() is from_cache=True.
        # Verify no -2 file was created by the query engine itself.
        assert not (queries_dir / "how-does-auth-work-2.md").exists()
        # Only the original file should exist
        assert list(queries_dir.iterdir()) == [queries_dir / "how-does-auth-work.md"]


# ---------------------------------------------------------------------------
# Test 8: AT-10 from_cache defaults to False
# ---------------------------------------------------------------------------

class TestFromCacheDefault:
    """AT-10: QueryResult.from_cache defaults to False."""

    def test_from_cache_default_false(self):
        """AT-10: Constructing QueryResult without from_cache gives False."""
        result = QueryResult(
            answer="Some answer.",
            sources=["src/auth.py.md"],
            one_line_summary="Explains auth.",
        )
        assert result.from_cache is False

    def test_cached_path_default_none(self):
        """cached_path defaults to None."""
        result = QueryResult(
            answer="Some answer.",
            sources=[],
            one_line_summary="",
        )
        assert result.cached_path is None

    def test_cached_at_default_none(self):
        """cached_at defaults to None."""
        result = QueryResult(
            answer="Some answer.",
            sources=[],
            one_line_summary="",
        )
        assert result.cached_at is None


# ---------------------------------------------------------------------------
# Test 9: AT-11 read_query_page exception → cache miss → full pipeline
# ---------------------------------------------------------------------------

class TestReadQueryPageExceptionResilient:
    """AT-11: If read_query_page raises, full pipeline runs; no exception propagates."""

    def test_read_query_page_exception_falls_through(self, tmp_path, monkeypatch):
        """AT-11: Exception in read_query_page treated as cache miss; full pipeline runs."""
        vault, codebase = _make_vault_with_index(
            tmp_path,
            index_rows=[
                "| [[src/auth.py]] | Auth module |\n",
                "| [[queries/how-does-auth-work]] | Explains auth |\n",
            ],
        )
        queries_dir = vault / "queries"
        queries_dir.mkdir()
        # Write a broken (no H1) query page that will cause read_query_page to raise ValueError
        broken_content = "No H1 here\n\n## Sources\n- src/auth.py.md\n"
        (queries_dir / "how-does-auth-work.md").write_text(broken_content, encoding="utf-8")
        config = make_config(codebase)

        monkeypatch.setattr("codebase_wiki_builder.query_engine._count_tokens", lambda t: 100)
        mock_llm = MagicMock()
        mock_llm.complete.side_effect = [
            # Stage 2 also tries to read the file, also fails → no candidates → miss
            # Full pipeline runs
            '["src/auth.py.md"]',
            json.dumps({
                "answer": "Auth pipeline answer.",
                "one_line_summary": "Auth explained.",
            }),
        ]

        # Should NOT raise; full pipeline should run
        result = run_query("How does auth work?", vault, mock_llm, config)

        assert result.from_cache is False
        assert "Auth pipeline answer." in result.answer


# ---------------------------------------------------------------------------
# Test 10: AT-12 Stage 2 LLM error resilience
# ---------------------------------------------------------------------------

class TestStage2LLMErrorResilient:
    """AT-12: LLMError in Stage 2 → cache miss → full pipeline runs."""

    def test_stage2_llm_error_falls_through(self, tmp_path, monkeypatch):
        """AT-12: If Stage 2 LLM raises, treat as miss; full two-LLM-call pipeline runs."""
        from codebase_wiki_builder.llm_client import LLMError

        vault, codebase = _make_vault_with_index(
            tmp_path,
            index_rows=[
                "| [[src/auth.py]] | Auth module |\n",
                "| [[queries/how-does-auth-work]] | Explains auth |\n",
            ],
        )
        queries_dir = vault / "queries"
        queries_dir.mkdir()
        _write_query_page(
            queries_dir / "how-does-auth-work.md",
            question="How does auth work?",
        )
        config = make_config(codebase)

        monkeypatch.setattr("codebase_wiki_builder.query_engine._count_tokens", lambda t: 100)

        # Simulate Stage 2 failing with LLMError, then full pipeline succeeding
        call_count = [0]
        def side_effect(prompt: str) -> str:
            call_count[0] += 1
            if call_count[0] == 1:
                # First call is Stage 2 pre-check — raise LLMError
                raise LLMError("Rate limit exceeded")
            elif call_count[0] == 2:
                return '["src/auth.py.md"]'
            else:
                return json.dumps({
                    "answer": "Fresh auth answer after error.",
                    "one_line_summary": "Auth fresh.",
                })

        mock_llm = MagicMock()
        mock_llm.complete.side_effect = side_effect

        # Must not raise
        result = run_query("Explain how authentication is handled", vault, mock_llm, config)

        assert result.from_cache is False
        assert "Fresh auth answer after error." in result.answer
        # 3 total calls: 1 Stage 2 error + 2 full pipeline
        assert mock_llm.complete.call_count == 3


# ---------------------------------------------------------------------------
# Test 11: AT-15 SEC-3 prefix check
# ---------------------------------------------------------------------------

class TestSEC3PrefixCheck:
    """AT-15: Stage 2 LLM returns path not starting with 'queries/' → cache miss."""

    def test_stage2_bad_prefix_is_cache_miss(self, tmp_path, monkeypatch):
        """AT-15: Path like '../sensitive/file' fails prefix check → full pipeline runs."""
        vault, codebase = _make_vault_with_index(
            tmp_path,
            index_rows=[
                "| [[src/auth.py]] | Auth module |\n",
                "| [[queries/how-does-auth-work]] | Explains auth |\n",
            ],
        )
        queries_dir = vault / "queries"
        queries_dir.mkdir()
        _write_query_page(
            queries_dir / "how-does-auth-work.md",
            question="How does auth work?",
        )
        config = make_config(codebase)

        monkeypatch.setattr("codebase_wiki_builder.query_engine._count_tokens", lambda t: 100)
        mock_llm = MagicMock()
        mock_llm.complete.side_effect = [
            # Stage 2: LLM returns a bad path (fails prefix check)
            "summaries/some-page",
            # Full pipeline
            '["src/auth.py.md"]',
            json.dumps({
                "answer": "Fallback answer.",
                "one_line_summary": "Fallback.",
            }),
        ]

        result = run_query("Explain how auth works", vault, mock_llm, config)

        # Cache miss → full pipeline ran
        assert result.from_cache is False
        assert mock_llm.complete.call_count == 3
        # Sensitive file must not exist on disk
        sensitive = vault / "summaries" / "some-page.md"
        assert not sensitive.exists()


# ---------------------------------------------------------------------------
# Test 12: AT-16 SEC-3 containment check
# ---------------------------------------------------------------------------

class TestSEC3ContainmentCheck:
    """AT-16: Path traversal that resolves outside vault_root/queries/ → cache miss."""

    def test_stage2_path_traversal_is_cache_miss(self, tmp_path, monkeypatch):
        """AT-16: queries/../../etc/passwd resolves outside queries/ → full pipeline."""
        vault, codebase = _make_vault_with_index(
            tmp_path,
            index_rows=[
                "| [[src/auth.py]] | Auth module |\n",
                "| [[queries/how-does-auth-work]] | Explains auth |\n",
            ],
        )
        queries_dir = vault / "queries"
        queries_dir.mkdir()
        _write_query_page(
            queries_dir / "how-does-auth-work.md",
            question="How does auth work?",
        )
        config = make_config(codebase)

        monkeypatch.setattr("codebase_wiki_builder.query_engine._count_tokens", lambda t: 100)
        mock_llm = MagicMock()
        mock_llm.complete.side_effect = [
            # Stage 2: path traversal attempt
            "queries/../../etc/passwd",
            # Full pipeline
            '["src/auth.py.md"]',
            json.dumps({
                "answer": "Secure answer.",
                "one_line_summary": "Security maintained.",
            }),
        ]

        result = run_query("Explain auth", vault, mock_llm, config)

        assert result.from_cache is False
        assert mock_llm.complete.call_count == 3
        # /etc/passwd should not have been accessed
        assert not (vault / "etc" / "passwd.md").exists()


# ---------------------------------------------------------------------------
# Test 13: AT-17 SEC-3 allowlist check
# ---------------------------------------------------------------------------

class TestSEC3AllowlistCheck:
    """AT-17: Valid path syntactically but not in pre-computed index allowlist → cache miss."""

    def test_stage2_unlisted_path_is_cache_miss(self, tmp_path, monkeypatch):
        """AT-17: Path passes prefix+containment but is not in index.md allowlist → miss."""
        vault, codebase = _make_vault_with_index(
            tmp_path,
            index_rows=[
                "| [[src/auth.py]] | Auth module |\n",
                "| [[queries/how-does-auth-work]] | Explains auth |\n",
            ],
        )
        queries_dir = vault / "queries"
        queries_dir.mkdir()
        _write_query_page(
            queries_dir / "how-does-auth-work.md",
            question="How does auth work?",
        )
        # Create an UNLISTED file inside queries/ (not in index.md)
        _write_query_page(
            queries_dir / "secret-internal-page.md",
            question="Secret internal question?",
            answer_body="Sensitive content.",
        )
        config = make_config(codebase)

        monkeypatch.setattr("codebase_wiki_builder.query_engine._count_tokens", lambda t: 100)
        mock_llm = MagicMock()
        mock_llm.complete.side_effect = [
            # Stage 2: returns a path that is syntactically valid but not in allowlist
            "queries/secret-internal-page",
            # Full pipeline
            '["src/auth.py.md"]',
            json.dumps({
                "answer": "Normal answer.",
                "one_line_summary": "Normal.",
            }),
        ]

        result = run_query("Explain auth", vault, mock_llm, config)

        # Allowlist check must have caught this → cache miss → full pipeline
        assert result.from_cache is False
        assert mock_llm.complete.call_count == 3
        # Answer should NOT contain content from the secret page
        assert "Sensitive content." not in result.answer
