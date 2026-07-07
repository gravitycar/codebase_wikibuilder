"""Configuration model and loader for Codebase Wiki Builder."""

from __future__ import annotations

import json
import os
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

# Module-level — runs once on first import
load_dotenv(override=False)


@dataclass
class WikiConfig:
    codebase_path: list[str] = field(default_factory=list)  # files and/or directories to ingest
    llm_provider: str = DEFAULT_PROVIDER                     # "anthropic" | "openai"
    llm_model: str = DEFAULT_MODEL_ANTHROPIC                 # model name string
    file_size_threshold: int = DEFAULT_FILE_SIZE_THRESHOLD
    inter_request_delay: float = DEFAULT_INTER_REQUEST_DELAY
    wiki_description: str = ""                               # optional: injected into MCP tool description


def get_codebase_root(config: WikiConfig) -> Path:
    """Return the common ancestor directory of all codebase_path entries.

    For a single directory entry, returns that directory. For a single file
    entry, returns its parent. For multiple entries, returns the deepest
    directory that contains all of them.
    """
    paths = config.codebase_path
    resolved = [Path(p).resolve() for p in paths]

    # Convert files → parent dir; keep directories as-is; treat nonexistent → as-is
    dir_paths = [
        str(p if (p.is_dir() or not p.exists()) else p.parent)
        for p in resolved
    ]

    if len(dir_paths) == 1:
        return Path(dir_paths[0])

    return Path(os.path.commonpath(dir_paths))


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

    # Normalize codebase_path: accept legacy string or new list format
    raw_codebase = raw.get("codebase_path", [])
    if isinstance(raw_codebase, str):
        raw_codebase = [raw_codebase] if raw_codebase else []
    elif not isinstance(raw_codebase, list):
        raw_codebase = []

    # Merge defaults before validation
    config = WikiConfig(
        codebase_path=raw_codebase,
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
            f"Config error: {config_path}: required field 'codebase_path' is missing or empty. "
            "Expected: a list of one or more absolute paths (files or directories).",
            file=sys.stderr,
        )
        sys.exit(1)

    for path_str in config.codebase_path:
        p = Path(path_str)
        if not p.exists():
            print(
                f"Config error: {config_path}: 'codebase_path' entry "
                f"'{path_str}' does not exist.",
                file=sys.stderr,
            )
            sys.exit(1)
        if not (p.is_file() or p.is_dir()):
            print(
                f"Config error: {config_path}: 'codebase_path' entry "
                f"'{path_str}' is not a readable file or directory.",
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

    # --- Step 1: codebase paths ---
    print("Step 1: Enter one or more paths to ingest (files or directories).")
    print("        Press Enter with no input when you are done.\n")
    collected: list[str] = []
    while True:
        prompt = f"  Path {len(collected) + 1}: " if not collected else f"  Path {len(collected) + 1} (or Enter to finish): "
        raw_path = input(prompt).strip()
        if not raw_path:
            if not collected:
                print("  At least one path is required. Please try again.")
                continue
            break
        p = Path(raw_path)
        if not p.exists():
            print(f"  '{raw_path}' does not exist. Please try again.")
            continue
        if not (p.is_file() or p.is_dir()):
            print(f"  '{raw_path}' is not a file or directory. Please try again.")
            continue
        collected.append(str(p.resolve()))

    # --- Step 2: LLM provider ---
    print(f"\nStep 2: Choose an LLM provider.")
    providers_display = " / ".join(f"[{i+1}] {p}" for i, p in enumerate(SUPPORTED_PROVIDERS))
    provider = DEFAULT_PROVIDER
    while True:
        raw = input(f"  Provider ({providers_display}, default: {DEFAULT_PROVIDER}): ").strip().lower()
        if not raw:
            break
        if raw in SUPPORTED_PROVIDERS:
            provider = raw
            break
        # Accept numeric shortcut
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(SUPPORTED_PROVIDERS):
                provider = SUPPORTED_PROVIDERS[idx]
                break
        except ValueError:
            pass
        print(f"  Invalid choice. Enter one of: {', '.join(SUPPORTED_PROVIDERS)}")

    # --- Step 3: model name ---
    default_model = DEFAULT_MODEL_ANTHROPIC if provider == "anthropic" else DEFAULT_MODEL_OPENAI
    print(f"\nStep 3: Enter the model name (default: {default_model}).")
    model = default_model
    while True:
        raw = input(f"  Model: ").strip()
        if not raw:
            break
        if raw:
            model = raw
            break

    # --- Step 4: wiki description ---
    print("\nStep 4: Enter a brief description for this wiki.")
    print("        This is shown to coding agents by the MCP server (optional, press Enter to skip).")
    wiki_description = input("  Description: ").strip()

    config = WikiConfig(
        codebase_path=collected,
        llm_provider=provider,
        llm_model=model,
        wiki_description=wiki_description,
    )
    save_config(config, vault_root)
    print(f"\nConfiguration saved to {vault_root / CONFIG_FILENAME}\n")
    return config
