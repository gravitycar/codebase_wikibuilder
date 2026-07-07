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
    get_codebase_root,
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
            "codebase_path": [str(codebase)],
            "llm_provider": "anthropic",
            "llm_model": "claude-sonnet-4-6",
            "file_size_threshold": 50000,
            "inter_request_delay": 0.5,
        }
        config_file = tmp_path / CONFIG_FILENAME
        config_file.write_text(json.dumps(config_data), encoding="utf-8")

        result = load_config(tmp_path)
        assert result.codebase_path == [str(codebase)]
        assert result.llm_provider == "anthropic"
        assert result.llm_model == "claude-sonnet-4-6"
        assert result.file_size_threshold == 50000
        assert result.inter_request_delay == 0.5

    def test_loads_legacy_string_codebase_path(self, tmp_path):
        # Legacy configs store codebase_path as a string — must be auto-wrapped in a list
        codebase = tmp_path / "myapp"
        codebase.mkdir()
        config_data = {"codebase_path": str(codebase)}
        config_file = tmp_path / CONFIG_FILENAME
        config_file.write_text(json.dumps(config_data), encoding="utf-8")

        result = load_config(tmp_path)
        assert result.codebase_path == [str(codebase)]

    def test_loads_multiple_codebase_paths(self, tmp_path):
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        config_data = {"codebase_path": [str(dir_a), str(dir_b)]}
        config_file = tmp_path / CONFIG_FILENAME
        config_file.write_text(json.dumps(config_data), encoding="utf-8")

        result = load_config(tmp_path)
        assert result.codebase_path == [str(dir_a), str(dir_b)]

    def test_loads_file_entry_in_codebase_path(self, tmp_path):
        f = tmp_path / "config.php"
        f.write_text("<?php", encoding="utf-8")
        config_data = {"codebase_path": [str(f)]}
        config_file = tmp_path / CONFIG_FILENAME
        config_file.write_text(json.dumps(config_data), encoding="utf-8")

        result = load_config(tmp_path)
        assert result.codebase_path == [str(f)]

    def test_uses_defaults_for_missing_optional_fields(self, tmp_path):
        codebase = tmp_path / "myapp"
        codebase.mkdir()
        config_data = {"codebase_path": [str(codebase)]}
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
            "codebase_path": [str(codebase)],
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
    def test_rejects_empty_codebase_path_list(self, tmp_path):
        config = WikiConfig(codebase_path=[])
        with pytest.raises(SystemExit) as exc_info:
            _validate(config, tmp_path / CONFIG_FILENAME)
        assert exc_info.value.code == 1

    def test_rejects_nonexistent_codebase_path(self, tmp_path):
        config = WikiConfig(codebase_path=[str(tmp_path / "nonexistent")])
        with pytest.raises(SystemExit) as exc_info:
            _validate(config, tmp_path / CONFIG_FILENAME)
        assert exc_info.value.code == 1

    def test_rejects_nonexistent_path_in_multi_entry_list(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        config = WikiConfig(codebase_path=[str(codebase), str(tmp_path / "missing")])
        with pytest.raises(SystemExit) as exc_info:
            _validate(config, tmp_path / CONFIG_FILENAME)
        assert exc_info.value.code == 1

    def test_accepts_file_entry(self, tmp_path):
        f = tmp_path / "config.php"
        f.write_text("<?php", encoding="utf-8")
        config = WikiConfig(codebase_path=[str(f)])
        # Should not raise
        _validate(config, tmp_path / CONFIG_FILENAME)

    def test_accepts_mixed_files_and_dirs(self, tmp_path):
        d = tmp_path / "src"
        d.mkdir()
        f = tmp_path / "config.php"
        f.write_text("<?php", encoding="utf-8")
        config = WikiConfig(codebase_path=[str(d), str(f)])
        # Should not raise
        _validate(config, tmp_path / CONFIG_FILENAME)

    def test_rejects_unsupported_provider(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        config = WikiConfig(codebase_path=[str(codebase)], llm_provider="unknown_provider")
        with pytest.raises(SystemExit) as exc_info:
            _validate(config, tmp_path / CONFIG_FILENAME)
        assert exc_info.value.code == 1

    def test_rejects_empty_model_name(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        config = WikiConfig(codebase_path=[str(codebase)], llm_model="")
        with pytest.raises(SystemExit) as exc_info:
            _validate(config, tmp_path / CONFIG_FILENAME)
        assert exc_info.value.code == 1

    def test_rejects_whitespace_only_model_name(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        config = WikiConfig(codebase_path=[str(codebase)], llm_model="   ")
        with pytest.raises(SystemExit) as exc_info:
            _validate(config, tmp_path / CONFIG_FILENAME)
        assert exc_info.value.code == 1

    def test_rejects_zero_file_size_threshold(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        config = WikiConfig(codebase_path=[str(codebase)], file_size_threshold=0)
        with pytest.raises(SystemExit) as exc_info:
            _validate(config, tmp_path / CONFIG_FILENAME)
        assert exc_info.value.code == 1

    def test_rejects_negative_file_size_threshold(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        config = WikiConfig(codebase_path=[str(codebase)], file_size_threshold=-1)
        with pytest.raises(SystemExit) as exc_info:
            _validate(config, tmp_path / CONFIG_FILENAME)
        assert exc_info.value.code == 1

    def test_rejects_negative_inter_request_delay(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        config = WikiConfig(codebase_path=[str(codebase)], inter_request_delay=-0.1)
        with pytest.raises(SystemExit) as exc_info:
            _validate(config, tmp_path / CONFIG_FILENAME)
        assert exc_info.value.code == 1

    def test_accepts_zero_delay(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        config = WikiConfig(codebase_path=[str(codebase)], inter_request_delay=0.0)
        # Should not raise
        _validate(config, tmp_path / CONFIG_FILENAME)

    def test_valid_config_passes(self, tmp_path):
        codebase = tmp_path / "app"
        codebase.mkdir()
        config = WikiConfig(
            codebase_path=[str(codebase)],
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
            codebase_path=[str(codebase)],
            llm_provider="openai",
            llm_model="gpt-4o",
            file_size_threshold=200_000,
            inter_request_delay=2.0,
        )
        save_config(config, tmp_path)
        loaded = load_config(tmp_path)
        assert loaded.codebase_path == [str(codebase)]
        assert loaded.llm_provider == "openai"
        assert loaded.llm_model == "gpt-4o"
        assert loaded.file_size_threshold == 200_000
        assert loaded.inter_request_delay == 2.0

    def test_saves_multiple_paths_and_reloads(self, tmp_path):
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        config = WikiConfig(codebase_path=[str(dir_a), str(dir_b)])
        save_config(config, tmp_path)
        loaded = load_config(tmp_path)
        assert loaded.codebase_path == [str(dir_a), str(dir_b)]


# ---------------------------------------------------------------------------
# get_codebase_root
# ---------------------------------------------------------------------------

class TestGetCodebaseRoot:
    def test_single_directory(self, tmp_path):
        d = tmp_path / "src"
        d.mkdir()
        config = WikiConfig(codebase_path=[str(d)])
        assert get_codebase_root(config) == d

    def test_single_file(self, tmp_path):
        f = tmp_path / "config.php"
        f.write_text("<?php")
        config = WikiConfig(codebase_path=[str(f)])
        assert get_codebase_root(config) == tmp_path

    def test_multiple_dirs_same_parent(self, tmp_path):
        dir_a = tmp_path / "src"
        dir_b = tmp_path / "frontend"
        dir_a.mkdir()
        dir_b.mkdir()
        config = WikiConfig(codebase_path=[str(dir_a), str(dir_b)])
        assert get_codebase_root(config) == tmp_path

    def test_dir_and_file_same_parent(self, tmp_path):
        d = tmp_path / "src"
        d.mkdir()
        f = tmp_path / "config.php"
        f.write_text("<?php")
        config = WikiConfig(codebase_path=[str(d), str(f)])
        assert get_codebase_root(config) == tmp_path

    def test_nested_dirs(self, tmp_path):
        parent = tmp_path / "app"
        child_a = parent / "src"
        child_b = parent / "lib"
        child_a.mkdir(parents=True)
        child_b.mkdir(parents=True)
        config = WikiConfig(codebase_path=[str(child_a), str(child_b)])
        assert get_codebase_root(config) == parent
