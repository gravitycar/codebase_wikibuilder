# Implementation Plan: Configuration Model and Loader

## Spec Context

This plan implements the configuration subsystem for the Codebase Wiki Builder. It fulfills FR-2 (all sub-requirements): reading and writing `.wiki-config.json`, validating all fields on load (hard-exit with code 1 on any error), prompting interactively for the codebase path when no config file exists, and loading LLM API keys from `.env` via `python-dotenv`. Every other catalog item (3–17) that touches config depends on this module.

Catalog item: 2 — Configuration Model and Loader
Specification section: FR-2 (Configuration), Technical Context (target stack, secrets policy)
Acceptance criteria addressed: FR-2 (config file location, interactive prompt on first run, validation with exit code 1 and informative messages, config fields and defaults, secrets in `.env` only, `.env` auto-loaded at runtime, two LLM providers)

## Dependencies

- **Blocked by**: Item 1 (Project Scaffold) — package must exist before this module can be placed inside it
- **Blocks**: Items 3–17 (every item that reads `WikiConfig`)
- **Uses**: `python-dotenv` (stdlib-augmenting; already in `pyproject.toml`), `dataclasses` (stdlib), `json` (stdlib), `pathlib` (stdlib), `sys` (stdlib), `os` (stdlib)

## File Changes

### New Files

- `codebase_wiki_builder/config.py` — `WikiConfig` dataclass, `load_config()`, `save_config()`, `prompt_for_config()`, validation helpers, `.env` loading

### Modified Files

- None (this is a new module; existing files from item 1 are untouched)

## Implementation Details

### `WikiConfig` Dataclass

**File**: `codebase_wiki_builder/config.py`

A plain Python `dataclass` (not pydantic) holds the config fields. Pydantic is not in the dependency list and would be an unnecessary addition for this small model. Validation is done in a dedicated `_validate()` function called by `load_config()` rather than in `__post_init__`, so validation errors can produce consistent, caller-controlled error messages.

**Exports**:
- `WikiConfig` — dataclass with all config fields and their defaults
- `load_config(vault_root: Path) -> WikiConfig` — loads and validates; exits 1 on any error
- `save_config(config: WikiConfig, vault_root: Path) -> None` — serializes to `.wiki-config.json`
- `prompt_for_config(vault_root: Path) -> WikiConfig` — interactive first-run setup; saves the resulting config
- `CONFIG_FILENAME: str` — constant `".wiki-config.json"` (used by callers that need to reference the path directly)

**Code Example**:

```python
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dotenv import load_dotenv

CONFIG_FILENAME = ".wiki-config.json"

SUPPORTED_PROVIDERS = ("anthropic", "openai")
DEFAULT_PROVIDER = "anthropic"
DEFAULT_MODEL_ANTHROPIC = "claude-sonnet-4-6"
DEFAULT_MODEL_OPENAI = "gpt-4o"
DEFAULT_FILE_SIZE_THRESHOLD = 100_000  # bytes
DEFAULT_INTER_REQUEST_DELAY = 1.0      # seconds


@dataclass
class WikiConfig:
    codebase_path: str                             # absolute path to target codebase
    llm_provider: str = DEFAULT_PROVIDER           # "anthropic" | "openai"
    llm_model: str = DEFAULT_MODEL_ANTHROPIC       # model name string
    file_size_threshold: int = DEFAULT_FILE_SIZE_THRESHOLD
    inter_request_delay: float = DEFAULT_INTER_REQUEST_DELAY
```

The `codebase_path` field has no default; it is always required in the JSON (or supplied interactively). All other fields have sensible defaults so that a minimal `.wiki-config.json` containing only `codebase_path` is valid.

### `.env` Loading

**Loaded at module import time** via a call to `load_dotenv()` at module level, so any module that does `from codebase_wiki_builder.config import load_config` will automatically trigger `.env` loading. `load_dotenv()` is called with `override=False` so that environment variables already set in the shell take precedence over the file.

```python
# Module-level — runs once on first import
load_dotenv(override=False)
```

`load_dotenv()` is a no-op if `.env` does not exist; this is the correct behavior (the file is optional). Supported keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) are read directly from the environment by `llm_client.py` (item 3); `config.py` does not read or store API keys.

### `load_config()`

**Signature**: `load_config(vault_root: Path) -> WikiConfig`

Reads `<vault_root>/.wiki-config.json`, parses JSON, constructs `WikiConfig`, then calls `_validate()`. If anything fails, prints an informative error to `stderr` and calls `sys.exit(1)`.

**Error conditions and messages** (all print to `stderr` then `sys.exit(1)`):

| Condition | Message format |
|-----------|---------------|
| File not found | `"Config error: {config_path} not found. Run 'codewiki ingest' to create it."` |
| Malformed JSON | `"Config error: {config_path} contains invalid JSON. Expected a JSON object with fields: codebase_path, llm_provider, llm_model, file_size_threshold, inter_request_delay."` |
| Missing required field (`codebase_path`) | `"Config error: {config_path}: required field 'codebase_path' is missing. Expected: absolute path string."` |
| Stale path (path not a readable directory) | `"Config error: {config_path}: field 'codebase_path' = '{value}' is not a readable directory. Expected: absolute path to an existing, readable directory."` |
| Invalid provider | `"Config error: {config_path}: field 'llm_provider' = '{value}' is not supported. Expected one of: anthropic, openai."` |
| Invalid model (empty string) | `"Config error: {config_path}: field 'llm_model' = '{value}' is invalid. Expected: non-empty model name string."` |
| Invalid file_size_threshold (not int or <= 0) | `"Config error: {config_path}: field 'file_size_threshold' = '{value}' is invalid. Expected: positive integer (bytes)."` |
| Invalid inter_request_delay (not numeric or < 0) | `"Config error: {config_path}: field 'inter_request_delay' = '{value}' is invalid. Expected: non-negative number (seconds)."` |

**Code Example**:

```python
def load_config(vault_root: Path) -> WikiConfig:
    config_path = vault_root / CONFIG_FILENAME
    if not config_path.exists():
        print(
            f"Config error: {config_path} not found. "
            "Run 'codewiki ingest' to create it.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(
            f"Config error: {config_path} contains invalid JSON. "
            "Expected a JSON object with fields: codebase_path, llm_provider, "
            "llm_model, file_size_threshold, inter_request_delay.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not isinstance(raw, dict):
        print(
            f"Config error: {config_path} contains invalid JSON. "
            "Expected a JSON object with fields: codebase_path, llm_provider, "
            "llm_model, file_size_threshold, inter_request_delay.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Merge defaults before validation
    config = WikiConfig(
        codebase_path=raw.get("codebase_path", ""),
        llm_provider=raw.get("llm_provider", DEFAULT_PROVIDER),
        llm_model=raw.get("llm_model", DEFAULT_MODEL_ANTHROPIC),
        file_size_threshold=raw.get("file_size_threshold", DEFAULT_FILE_SIZE_THRESHOLD),
        inter_request_delay=raw.get("inter_request_delay", DEFAULT_INTER_REQUEST_DELAY),
    )
    _validate(config, config_path)
    return config
```

`_validate()` checks each field in sequence, printing the first error it finds and calling `sys.exit(1)`. It does not accumulate multiple errors — failing fast on the first invalid field is clearer for users.

### `_validate()`

**Signature**: `_validate(config: WikiConfig, config_path: Path) -> None`

Internal helper; not exported. Checks fields in this order:
1. `codebase_path` — must be a non-empty string. Construct `Path(config.codebase_path)` and check `is_dir()`. If the path does not exist or is not a directory: print the stale-path error and `sys.exit(1)`.
2. `llm_provider` — must be in `SUPPORTED_PROVIDERS`.
3. `llm_model` — must be a non-empty string.
4. `file_size_threshold` — must be an `int` and `> 0`.
5. `inter_request_delay` — must be `int` or `float` and `>= 0`.

```python
def _validate(config: WikiConfig, config_path: Path) -> None:
    if not config.codebase_path:
        print(
            f"Config error: {config_path}: required field 'codebase_path' is missing. "
            "Expected: absolute path string.",
            file=sys.stderr,
        )
        sys.exit(1)

    codebase = Path(config.codebase_path)
    if not codebase.is_dir():
        print(
            f"Config error: {config_path}: field 'codebase_path' = "
            f"'{config.codebase_path}' is not a readable directory. "
            "Expected: absolute path to an existing, readable directory.",
            file=sys.stderr,
        )
        sys.exit(1)

    if config.llm_provider not in SUPPORTED_PROVIDERS:
        print(
            f"Config error: {config_path}: field 'llm_provider' = "
            f"'{config.llm_provider}' is not supported. "
            f"Expected one of: {', '.join(SUPPORTED_PROVIDERS)}.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not isinstance(config.llm_model, str) or not config.llm_model.strip():
        print(
            f"Config error: {config_path}: field 'llm_model' = "
            f"'{config.llm_model}' is invalid. "
            "Expected: non-empty model name string.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not isinstance(config.file_size_threshold, int) or config.file_size_threshold <= 0:
        print(
            f"Config error: {config_path}: field 'file_size_threshold' = "
            f"'{config.file_size_threshold}' is invalid. "
            "Expected: positive integer (bytes).",
            file=sys.stderr,
        )
        sys.exit(1)

    if not isinstance(config.inter_request_delay, (int, float)) or config.inter_request_delay < 0:
        print(
            f"Config error: {config_path}: field 'inter_request_delay' = "
            f"'{config.inter_request_delay}' is invalid. "
            "Expected: non-negative number (seconds).",
            file=sys.stderr,
        )
        sys.exit(1)
```

### `save_config()`

**Signature**: `save_config(config: WikiConfig, vault_root: Path) -> None`

Serializes `WikiConfig` to JSON and writes to `<vault_root>/.wiki-config.json`. Uses `dataclasses.asdict()` for serialization. Writes with `indent=2` for human readability. Never writes API keys (they live only in `.env`).

```python
def save_config(config: WikiConfig, vault_root: Path) -> None:
    config_path = vault_root / CONFIG_FILENAME
    config_path.write_text(
        json.dumps(asdict(config), indent=2),
        encoding="utf-8",
    )
```

No validation is performed on save — callers are responsible for saving valid configs. In practice, `prompt_for_config()` always saves a freshly-validated config.

### `prompt_for_config()`

**Signature**: `prompt_for_config(vault_root: Path) -> WikiConfig`

Called by the `ingest` CLI command when `.wiki-config.json` does not exist. Prompts the user interactively for the target codebase path. Validates the path before accepting it. Builds a `WikiConfig` with all other fields at their defaults, saves it, and returns it.

```python
def prompt_for_config(vault_root: Path) -> WikiConfig:
    print("No configuration file found. Let's set up your wiki.")
    print(f"Config will be saved to: {vault_root / CONFIG_FILENAME}\n")

    while True:
        raw_path = input("Enter the absolute path to your target codebase: ").strip()
        if not raw_path:
            print("  Path cannot be empty. Please try again.")
            continue
        codebase = Path(raw_path)
        if not codebase.is_dir():
            print(f"  '{raw_path}' is not a readable directory. Please try again.")
            continue
        break

    config = WikiConfig(codebase_path=str(codebase.resolve()))
    save_config(config, vault_root)
    print(f"Configuration saved to {vault_root / CONFIG_FILENAME}\n")
    return config
```

Only `codebase_path` is prompted for; all other fields use defaults. Users who need to change provider/model/threshold can edit `.wiki-config.json` directly after creation. The path is stored as `str(codebase.resolve())` (absolute, normalized) to avoid relative-path issues on future runs.

## Error Handling

- **File not found in `load_config()`**: This is only an error when called from commands other than `ingest`. The `ingest` command checks for the file's existence itself and calls `prompt_for_config()` instead of `load_config()` when absent. The `load_config()` error path covers the case where a user runs `codewiki analysis` or `codewiki query` before ever running `codewiki ingest`.
- **`sys.exit(1)`** is called directly from `load_config()` and `_validate()`. This is intentional: config errors are fatal, and using exceptions here would require every caller to catch and re-raise them with the same exit code. Direct `sys.exit(1)` is cleaner for a CLI tool.
- **`prompt_for_config()` does not call `sys.exit()`** — it loops until a valid path is provided (or the user sends EOF/Ctrl-C, which raises `EOFError`/`KeyboardInterrupt` naturally and exits the process).
- **JSON type coercion**: If `file_size_threshold` is stored in the JSON as a float (e.g., `100000.0`), `isinstance(..., int)` will return `False` and validation will fail. The spec requires this field to be a positive integer; users must store it as an integer in the JSON.

## Unit Test Specifications

**File**: `tests/test_config.py`

### `load_config()` — file-not-found

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Config missing | No `.wiki-config.json` in temp dir | `sys.exit(1)` called; stderr contains config path | FR-2: missing config is fatal |

### `load_config()` — malformed JSON

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Truncated JSON | Write `{"codebase_path":` to config | `sys.exit(1)`; stderr mentions "invalid JSON" and expected fields | FR-2: malformed JSON is fatal |
| Non-object JSON | Write `[1, 2, 3]` to config | `sys.exit(1)`; stderr mentions "invalid JSON" | JSON must be an object |

### `load_config()` — field validation

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Missing `codebase_path` | JSON `{}` | `sys.exit(1)`; stderr mentions `codebase_path`, "missing", "absolute path string" | Required field |
| Stale path | `codebase_path` points to non-existent dir | `sys.exit(1)`; stderr includes the path value and "not a readable directory" | FR-2: stale path is hard error |
| Path is a file, not dir | `codebase_path` points to a file | `sys.exit(1)`; stderr includes "not a readable directory" | Must be a directory |
| Invalid provider | `llm_provider = "gemini"` | `sys.exit(1)`; stderr mentions `llm_provider`, "gemini", "anthropic, openai" | FR-2: unsupported provider |
| Empty model | `llm_model = ""` | `sys.exit(1)`; stderr mentions `llm_model`, "non-empty" | FR-2: invalid field |
| Zero threshold | `file_size_threshold = 0` | `sys.exit(1)`; stderr mentions `file_size_threshold`, "positive integer" | FR-2: invalid field |
| Negative delay | `inter_request_delay = -1` | `sys.exit(1)`; stderr mentions `inter_request_delay`, "non-negative" | FR-2: invalid field |
| Float threshold | `file_size_threshold = 100000.0` | `sys.exit(1)`; stderr mentions `file_size_threshold` | Must be int |

### `load_config()` — happy path and defaults

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Minimal valid config | JSON `{"codebase_path": "/tmp"}` (where `/tmp` is readable dir) | Returns `WikiConfig` with defaults for all other fields | Fields are optional with defaults |
| Full valid config | All fields present and valid | Returns `WikiConfig` matching all provided values | Round-trip fidelity |
| Provider `openai` | `{"codebase_path": "/tmp", "llm_provider": "openai", "llm_model": "gpt-4o", ...}` | Returns `WikiConfig` with provider `openai` | Both providers valid |

### `save_config()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Round-trip | Create `WikiConfig`, save, reload | Loaded config equals original | Serialization fidelity |
| Output is valid JSON | Save any config | `json.loads(config_path.read_text())` succeeds | File must be parseable |
| No API keys in file | Save config | JSON does not contain any key matching `*API_KEY*` | FR-2: secrets policy |

### `prompt_for_config()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Valid path on first try | Mock `input()` → valid dir path | Returns `WikiConfig` with that `codebase_path`; file saved | Happy path |
| Invalid then valid | Mock `input()` → non-existent path, then valid dir path | Loops; returns config on second attempt | Retry logic |
| Empty then valid | Mock `input()` → `""`, then valid dir path | Loops; returns config on second attempt | Empty path rejected |
| Path stored as absolute | Mock `input()` → relative path `"."` | `codebase_path` in saved config is an absolute path | `resolve()` called |

### `.env` loading

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| `.env` with ANTHROPIC_API_KEY | Write `.env` with `ANTHROPIC_API_KEY=sk-test`, import config | `os.environ["ANTHROPIC_API_KEY"] == "sk-test"` | `.env` loaded at import |
| Shell var takes precedence | Set `ANTHROPIC_API_KEY=shell-val` in env, `.env` has different value | Shell value preserved | `override=False` |

### Key Scenario: Stale Path Error Message

**Setup**: Create a temp vault dir. Write `.wiki-config.json` with `{"codebase_path": "/nonexistent/path/that/does/not/exist"}`.

**Action**: Call `load_config(vault_root)`.

**Expected**: Process exits with code 1 (captured via `pytest.raises(SystemExit) as exc_info`; `exc_info.value.code == 1`). The captured `stderr` output contains all three required elements: the config file path, the field name `codebase_path`, and the phrase "not a readable directory".

```python
import pytest
from pathlib import Path

def test_stale_path_error(tmp_path, capsys):
    config_path = tmp_path / ".wiki-config.json"
    config_path.write_text('{"codebase_path": "/nonexistent/path/xyz"}')

    with pytest.raises(SystemExit) as exc_info:
        from codebase_wiki_builder.config import load_config
        load_config(tmp_path)

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert str(config_path) in captured.err
    assert "codebase_path" in captured.err
    assert "not a readable directory" in captured.err
```

### Key Scenario: `prompt_for_config()` retry on bad path

```python
from unittest.mock import patch

def test_prompt_retries_on_bad_path(tmp_path):
    valid_dir = tmp_path / "codebase"
    valid_dir.mkdir()

    inputs = iter(["/does/not/exist", str(valid_dir)])
    with patch("builtins.input", side_effect=inputs):
        from codebase_wiki_builder.config import prompt_for_config
        config = prompt_for_config(tmp_path)

    assert config.codebase_path == str(valid_dir.resolve())
    assert (tmp_path / ".wiki-config.json").exists()
```

## Notes

- **No pydantic**: The spec's dependency list does not include pydantic. A plain `dataclass` with a separate `_validate()` function provides all required behavior with zero additional dependencies.
- **`sys.exit(1)` in library code**: Using `sys.exit()` directly in a library function is normally bad practice, but this module is specifically a CLI configuration loader — it is never imported by anything other than the CLI entry point. The spec explicitly mandates exit code 1 with an informative message, making direct exit the correct approach.
- **`load_dotenv()` at module import time**: This is a deliberate design choice so that any module importing `config.py` automatically gets `.env` loaded. The alternative (calling `load_dotenv()` explicitly in `cli.py`) would require every entry point to remember to call it. Since `.env` loading is always required, doing it at import is simpler and less error-prone.
- **`.wiki-config.json` is in `.gitignore`**: The spec mandates this (security requirement). The project scaffold (item 1) pre-configured `.gitignore` for Python — the `ingest` CLI command should print a reminder to add `.wiki-config.json` and `.env` to `.gitignore` if they are not already listed. That check lives in the CLI (item 9), not here.
- **`codebase_path` stored as `str`, not `Path`**: Storing as `str` simplifies JSON serialization (no custom JSON encoder needed). Callers that need a `Path` object should call `Path(config.codebase_path)` themselves. This is a minor ergonomic tradeoff — callers are few and explicit conversion is clear.
- **Test isolation for `.env` loading**: Tests that check `.env` loading must be careful about module import order, because `load_dotenv()` runs at import time. Use `importlib.reload()` or ensure the test sets environment variables before the module is imported for the first time. Using `pytest-monkeypatch` to set/unset env vars is the recommended approach.
- **`inter_request_delay = 0`**: A delay of exactly 0 seconds is valid (no sleep between requests). The validation accepts `>= 0`.
