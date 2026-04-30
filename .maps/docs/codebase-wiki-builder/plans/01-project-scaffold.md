# Implementation Plan: Project Scaffold and Package Manifest

## Spec Context

This plan establishes the installable Python package skeleton for the Codebase Wiki Builder. It fulfills FR-1 (the two CLI entry points `codewiki` and `wiki-mcp` declared in `[project.scripts]`) and the Technical Context section's `pyproject.toml` structure requirements. Every other catalog item depends on this plan completing first — nothing can be imported or installed until the package manifest and package marker exist.

Catalog item: 1 — Project Scaffold and Package Manifest
Specification section: FR-1 (CLI entry points), Technical Context (pyproject.toml structure, dependency list, target stack)
Acceptance criteria addressed: FR-1 entry points; all runtime dependencies present; Ruff, mypy, and pytest configured.

## Dependencies

- **Blocked by**: None (this is the root item)
- **Blocks**: All other catalog items (2–18)
- **Uses**: `uv` package manager; Python 3.10+

## File Changes

### New Files

- `pyproject.toml` — Full project manifest: metadata, runtime dependencies, `[project.scripts]`, `[tool.ruff]`, `[tool.mypy]`, `[tool.pytest.ini_options]`
- `codebase_wiki_builder/__init__.py` — Package marker; exposes `__version__`

## Implementation Details

### `pyproject.toml`

**File**: `pyproject.toml` (project root)

The file uses the standard PEP 517/518 layout managed by `uv`. Build backend is `hatchling` (uv's default). All tool configuration lives in this single file — no `setup.cfg`, no `tox.ini`.

**Code Example**:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "codebase-wiki-builder"
version = "0.1.0"
description = "LLM-powered Obsidian wiki builder for codebases"
requires-python = ">=3.10"
dependencies = [
    "anthropic>=0.25.0",
    "openai>=1.30.0",
    "typer>=0.12.0",
    "python-dotenv>=1.0.0",
    "rich>=13.0.0",
    "tiktoken>=0.7.0",
    "tenacity>=8.3.0",
    "mcp>=1.0.0",
]

[project.scripts]
codewiki = "codebase_wiki_builder.cli:app"
wiki-mcp = "codebase_wiki_builder.mcp_server:main"

[tool.hatch.build.targets.wheel]
packages = ["codebase_wiki_builder"]

[tool.ruff]
target-version = "py310"
line-length = 100
select = ["E", "F", "I", "UP", "B", "SIM"]
ignore = ["E501"]

[tool.ruff.isort]
known-first-party = ["codebase_wiki_builder"]

[tool.mypy]
python_version = "3.10"
strict = true
ignore_missing_imports = true
warn_return_any = true
warn_unused_ignores = true

[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
python_classes = ["Test*"]
python_functions = ["test_*"]
addopts = "-v --tb=short"
```

**Key decisions**:

- `anthropic>=0.25.0` — primary LLM SDK (spec constraint: use native Anthropic SDK, not OpenAI as primary)
- `openai>=1.30.0` — optional alternative backend; listed as a runtime dependency so it is always available (avoids conditional import errors when provider switching)
- `typer>=0.12.0` — CLI framework (spec constraint: use Typer, not Click/argparse directly)
- `rich>=13.0.0` — terminal output; bundled with Typer but pinned explicitly for clarity
- `tiktoken>=0.7.0` — token counting for `ANALYSIS_CONTEXT_WINDOW` and `QUERY_CONTEXT_WINDOW` budget management
- `tenacity>=8.3.0` — retry logic with exponential backoff for LLM rate-limit handling (FR-3.4); simpler than writing manual retry loops
- `mcp>=1.0.0` — Python MCP SDK for stdio transport (FR-9.4); version pinned loosely to allow patch updates
- `python-dotenv>=1.0.0` — `.env` loading at runtime (FR-2)
- Ruff `select` rules: `E`/`F` (pycodestyle/pyflakes), `I` (isort), `UP` (pyupgrade), `B` (bugbear), `SIM` (simplify); `E501` ignored because line-length=100 is enforced by formatter
- mypy `strict = true` catches missing return types, untyped functions, and `Any` propagation; `ignore_missing_imports = true` prevents noise from stubs-less third-party packages
- pytest `testpaths = ["tests"]` keeps test discovery from scanning the package source tree

### `codebase_wiki_builder/__init__.py`

**File**: `codebase_wiki_builder/__init__.py`

A minimal package marker that exposes a `__version__` string. This makes the package importable and allows other modules to reference the version without reading `pyproject.toml` at runtime.

**Code Example**:

```python
"""Codebase Wiki Builder — LLM-powered Obsidian wiki generator."""

__version__ = "0.1.0"
```

No other symbols are exported from `__init__.py`. All public API lives in submodules (`cli`, `config`, `llm_client`, etc.) imported directly by consumers.

## Error Handling

This plan contains no runtime logic, so there are no runtime error conditions. The only failure mode is a `uv sync` or `uv pip install -e .` failure if a dependency version is unavailable — resolve by relaxing the lower-bound pin.

## Unit Test Specifications

This catalog item has no runtime logic to test. The package marker and manifest are validated indirectly by every other item's tests (which import from `codebase_wiki_builder`).

One smoke test confirms the package is importable and `__version__` is present:

### `test_package_importable`

**File**: `tests/test_scaffold.py`

| Case | Action | Expected | Why |
|------|--------|----------|-----|
| Package importable | `import codebase_wiki_builder` | No ImportError | Confirms `__init__.py` and package structure are correct |
| Version string present | `codebase_wiki_builder.__version__` | Non-empty string matching `\d+\.\d+\.\d+` | Confirms version is exported |
| Entry point `codewiki` registered | `importlib.metadata.entry_points(group="console_scripts")` | Contains entry named `codewiki` | Confirms `[project.scripts]` is installed |
| Entry point `wiki-mcp` registered | Same query | Contains entry named `wiki-mcp` | Confirms MCP entry point declared |

**Key scenario: Entry points registered**

```python
import importlib.metadata

def test_entry_points():
    eps = {ep.name for ep in importlib.metadata.entry_points(group="console_scripts")}
    assert "codewiki" in eps
    assert "wiki-mcp" in eps
```

This test must be run after `uv pip install -e .` (or `uv sync`) — it validates the installed metadata, not just the source tree.

## Notes

- The `codebase_wiki_builder/` directory must be created with `__init__.py` before any other module files are added (items 2–18 all place their files inside this package).
- `uv` is the project's package manager. Developers should run `uv sync` (or `uv pip install -e .`) after creating `pyproject.toml` to create the virtual environment and install all dependencies.
- `openai` is listed as a required dependency (not optional) to avoid `ImportError` at runtime when a user switches `provider` to `openai` in `.wiki-config.json`. The alternative (`importlib.import_module` with a try/except) adds unnecessary complexity for a small dependency.
- Do not add `[tool.ruff.format]` section — `ruff format` defaults are acceptable and the spec does not mandate specific formatter options.
- The `tests/` directory (referenced by `pytest.ini_options`) does not need to exist yet; pytest will simply find no tests if the directory is absent. It should be created (with `tests/__init__.py`) by whichever item first adds unit tests.
- Keep `__version__` in sync with `pyproject.toml` manually for MVP. If version drift becomes a concern in future, switch to `importlib.metadata.version("codebase-wiki-builder")` in `__init__.py`.
