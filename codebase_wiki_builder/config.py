"""Configuration model and loader for Codebase Wiki Builder."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from dotenv import load_dotenv

CONFIG_FILENAME = ".wiki-config.json"

SUPPORTED_PROVIDERS = ("anthropic", "openai")
DEFAULT_PROVIDER = "anthropic"
DEFAULT_MODEL_ANTHROPIC = "claude-sonnet-4-6"
DEFAULT_MODEL_OPENAI = "gpt-4o"
DEFAULT_FILE_SIZE_THRESHOLD = 100_000  # bytes
DEFAULT_INTER_REQUEST_DELAY = 1.0      # seconds

# Module-level — runs once on first import
load_dotenv(override=False)


@dataclass
class WikiConfig:
    codebase_path: str                             # absolute path to target codebase
    llm_provider: str = DEFAULT_PROVIDER           # "anthropic" | "openai"
    llm_model: str = DEFAULT_MODEL_ANTHROPIC       # model name string
    file_size_threshold: int = DEFAULT_FILE_SIZE_THRESHOLD
    inter_request_delay: float = DEFAULT_INTER_REQUEST_DELAY
    wiki_description: str = ""                     # optional: injected into MCP tool description


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
        wiki_description=str(raw.get("wiki_description", "")),
    )
    _validate(config, config_path)
    return config


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


def save_config(config: WikiConfig, vault_root: Path) -> None:
    config_path = vault_root / CONFIG_FILENAME
    config_path.write_text(
        json.dumps(asdict(config), indent=2),
        encoding="utf-8",
    )


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
