"""Unit tests for codebase_wiki_builder.config module."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from codebase_wiki_builder.config import (
    CONFIG_FILENAME,
    DEFAULT_FILE_SIZE_THRESHOLD,
    DEFAULT_INTER_REQUEST_DELAY,
    DEFAULT_MODEL_ANTHROPIC,
    DEFAULT_PROVIDER,
    WikiConfig,
    _validate,
    load_config,
    save_config,
)


# ---------------------------------------------------------------------------
# load_config — happy path
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_loads_valid_config(self, tmp_path):
        # Create a real target directory so _validate() can check it
        codebase = tmp_path / "myapp"
        codebase.mkdir()

        config_data = {
            "codebase_path": str(codebase),
            "llm_provider": "anthropic",
            "llm_model": "claude-sonnet-4-6",
            "file_size_threshold": 50000,
            "inter_request_delay": 0.5,
        }
        config_file = tmp_path / CONFIG_FILENAME
        config_file.write_text(json.dumps(config_data), encoding="utf-8")

        result = load_config(tmp_path)
        assert result.codebase_path == str(codebase)
        assert result.llm_provider == "anthropic"
        assert result.llm_model == "claude-sonnet-4-6"
        assert result.file_size_threshold == 50000
        assert result.inter_request_delay == 0.5

    def test_uses_defaults_for_missing_optional_fields(self, tmp_path):
        codebase = tmp_path / "myapp"
        codebase.mkdir()
        config_data = {"codebase_path": str(codebase)}
        config_file = tmp_path / CONFIG_FILENAME
        config_file.write_text(json.dumps(config_data), encoding="utf-8")

        result = load_config(tmp_path)
        assert result.llm_provider == DEFAULT_PROVIDER
        assert result.llm_model == DEFAULT_MODEL_ANTHROPIC
        assert result.file_size_threshold == DEFAULT_FILE_SIZE_THRESHOLD
        assert result.inter_request_delay == DEFAULT_INTER_REQUEST_DELAY

    def test_exits_when_config_file_missing(self, tmp_path):
        # No config file created
        with pytest.raises(SystemExit) as exc_info:
            load_config(tmp_path)
        assert exc_info.value.code == 1

    def test_exits_on_invalid_json(self, tmp_path):
        config_file = tmp_path / CONFIG_FILENAME
        config_file.write_text("NOT JSON {{", encoding="utf-8")
        with pytest.raises(SystemExit) as exc_info:
            load_config(tmp_path)
        assert exc_info.value.code == 1

    def test_exits_when_json_is_not_dict(self, tmp_path):
        config_file = tmp_path / CONFIG_FILENAME
        config_file.write_text('["not", "a", "dict"]', encoding="utf-8")
        with pytest.raises(SystemExit) as exc_info:
            load_config(tmp_path)
        assert exc_info.value.code == 1

    def test_openai_provider_accepted(self, tmp_path):
        codebase = tmp_path / "myapp"
        codebase.mkdir()
        config_data = {
            "codebase_path": str(codebase),
            "llm_provider": "openai",
            "llm_model": "gpt-4o",
        }
        config_file = tmp_path / CONFIG_FILENAME
        config_file.write_text(json.dumps(config_data), encoding="utf-8")

        result = load_config(tmp_path)
        assert result.llm_provider == "openai"


# ---------------------------------------------------------------------------
# _validate — error cases
# ---------------------------------------------------------------------------

class TestValidate:
    def test_rejects_empty_codebase_path(self, tmp_path):
        config = WikiConfig(codebase_path="")
        with pytest.raises(SystemExit) as exc_info:
            _validate(config, tmp_path / CONFIG_FILENAME)
        assert exc_info.value.code == 1

    def test_rejects_nonexistent_codebase_path(self, tmp_path):
        config = WikiConfig(codebase_path=str(tmp_path / "nonexistent"))
        with pytest.raises(SystemExit) as exc_info:
            _validate(config, tmp_path / CONFIG_FILENAME)
        assert exc_info.value.code == 1

    def test_rejects_unsupported_provider(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        config = WikiConfig(codebase_path=str(codebase), llm_provider="unknown_provider")
        with pytest.raises(SystemExit) as exc_info:
            _validate(config, tmp_path / CONFIG_FILENAME)
        assert exc_info.value.code == 1

    def test_rejects_empty_model_name(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        config = WikiConfig(codebase_path=str(codebase), llm_model="")
        with pytest.raises(SystemExit) as exc_info:
            _validate(config, tmp_path / CONFIG_FILENAME)
        assert exc_info.value.code == 1

    def test_rejects_whitespace_only_model_name(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        config = WikiConfig(codebase_path=str(codebase), llm_model="   ")
        with pytest.raises(SystemExit) as exc_info:
            _validate(config, tmp_path / CONFIG_FILENAME)
        assert exc_info.value.code == 1

    def test_rejects_zero_file_size_threshold(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        config = WikiConfig(codebase_path=str(codebase), file_size_threshold=0)
        with pytest.raises(SystemExit) as exc_info:
            _validate(config, tmp_path / CONFIG_FILENAME)
        assert exc_info.value.code == 1

    def test_rejects_negative_file_size_threshold(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        config = WikiConfig(codebase_path=str(codebase), file_size_threshold=-1)
        with pytest.raises(SystemExit) as exc_info:
            _validate(config, tmp_path / CONFIG_FILENAME)
        assert exc_info.value.code == 1

    def test_rejects_negative_inter_request_delay(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        config = WikiConfig(codebase_path=str(codebase), inter_request_delay=-0.1)
        with pytest.raises(SystemExit) as exc_info:
            _validate(config, tmp_path / CONFIG_FILENAME)
        assert exc_info.value.code == 1

    def test_accepts_zero_delay(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        config = WikiConfig(codebase_path=str(codebase), inter_request_delay=0.0)
        # Should not raise
        _validate(config, tmp_path / CONFIG_FILENAME)

    def test_valid_config_passes(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        config = WikiConfig(
            codebase_path=str(codebase),
            llm_provider="anthropic",
            llm_model="claude-sonnet-4-6",
            file_size_threshold=100_000,
            inter_request_delay=1.0,
        )
        # Should not raise
        _validate(config, tmp_path / CONFIG_FILENAME)


# ---------------------------------------------------------------------------
# save_config
# ---------------------------------------------------------------------------

class TestSaveConfig:
    def test_saves_and_reloads(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        config = WikiConfig(
            codebase_path=str(codebase),
            llm_provider="openai",
            llm_model="gpt-4o",
            file_size_threshold=200_000,
            inter_request_delay=2.0,
        )
        save_config(config, tmp_path)
        loaded = load_config(tmp_path)
        assert loaded.codebase_path == str(codebase)
        assert loaded.llm_provider == "openai"
        assert loaded.llm_model == "gpt-4o"
        assert loaded.file_size_threshold == 200_000
        assert loaded.inter_request_delay == 2.0
