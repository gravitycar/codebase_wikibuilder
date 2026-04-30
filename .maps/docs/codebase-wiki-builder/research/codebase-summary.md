# Codebase Summary: Codebase Wiki Builder

## Tech Stack

- **Language**: Python (inferred from `.gitignore`, which is a comprehensive Python-ecosystem gitignore covering pip, poetry, pdm, pipenv, uv, pyenv, pytest, mypy, ruff, pyinstaller, Django, Flask, Scrapy, Jupyter, Streamlit, Marimo, etc.)
- **No source code exists yet** — this is a greenfield project. No `pyproject.toml`, `setup.py`, `requirements.txt`, `Pipfile`, or any Python source files are present.
- **Tooling signals from `.gitignore`**:
  - Linter: Ruff (`.ruff_cache/` excluded)
  - Type checker: mypy (`.mypy_cache/` excluded)
  - Test runner: pytest (`.pytest_cache/`, `htmlcov/`, `coverage.xml` excluded)
  - Package management: multiple tools acknowledged (pip, poetry, pdm, uv, pipenv, pixi)
  - Env management: `.env` / `.venv` excluded (standard dotenv pattern for secrets)

## Architecture Overview

The project is entirely pre-code. The repository currently contains only:

- **MAPS workflow tooling** (`.maps/`, `.claude/`, `.mcp.json`) — the agentic programming system used to build the project, not part of the deliverable
- **A stub README** (`README.md` — contains only the project name)
- **An anomalous empty `--help/` directory** (appears to be an artifact of a mistyped shell command; contains only an empty `.maps/` subdirectory; no content)
- **One initial git commit** (`3ea11df Initial commit`)

There is no source code, no package manifest, no directory structure for the application itself. Everything is to be designed and built from scratch.

## Relevant Existing Code

None. There are no source files, modules, classes, utility functions, or configuration files related to the application under development.

## Project Intent (from Initial_Spec.md)

The application to be built is a CLI tool that:

1. **Scans** a target codebase directory recursively
2. **Summarizes** each file using the OpenAI API (LLM-generated markdown summaries)
3. **Writes** summaries to a corresponding directory tree in an Obsidian vault
4. **Tracks staleness** via MD5 hashes embedded in summary files — re-summarizes only changed files
5. **Generates cross-references** (Obsidian backlinks) showing which files reference each target file
6. **Handles deletions** — removes stale summary files and cleans up backlinks when source files are deleted
7. **Produces an `index.md`** (catalog of all wiki pages with one-line descriptions)
8. **Produces an `overview.md`** (higher-level analysis of the target application's patterns and purpose)
9. **Maintains two logs**: a human-readable `log.md` (append-only, timestamped) and a standard rotating log file under `logs/`
10. **Supports three CLI commands**: `ingest`, `analysis`, `query`

Key external integrations:
- **OpenAI API** for file summarization and analysis (API key + URL in `.env`)
- **Obsidian vault** as the output target (filesystem directory; may leverage Obsidian CLI for plugin management)

Configuration:
- `Target Codebase` (absolute path to source being wikified)
- `Obsidian Vault` (absolute path to output vault directory)
- `OpenAI API KEY` (secret, in `.env`)
- `OpenAI URL` (in `.env`)
- On first run: prompt user for target codebase path, record it (possibly in `index.md`)

## Conventions to Follow

Since no code exists yet, conventions should be established fresh. However, the `.gitignore` signals strong alignment with Python ecosystem norms:

- **Secrets management**: `.env` file (already in `.gitignore`) — use `python-dotenv` or similar
- **Testing**: pytest (already anticipated in `.gitignore`)
- **Linting**: Ruff (already in `.gitignore`)
- **Type checking**: mypy (already in `.gitignore`)
- **Environment isolation**: `.venv` (already in `.gitignore`)
- **No framework-specific scaffolding** indicated (no Django/Flask/Scrapy artifacts committed)

The spec explicitly calls out:
- CLI interface (standard Python CLI pattern — likely `argparse`, `click`, or `typer`)
- Local single-user execution (no server, no auth, minimal security hardening needed)
- MD5 hashing for change detection
- Obsidian markdown with backlink syntax (`[[filename]]`)
- Timestamped log entries in `YYYY-MM-DD H:m:s` format

## Reusable Components

None exist yet. All components are to be designed and built.

## Anomalies and Notes for the Architect

1. **`--help/` directory**: An empty directory literally named `--help` exists at the project root. This appears to be an artifact of a shell command like `mkdir --help` being misinterpreted. It should be removed or ignored; it is not part of the application design.

2. **No package manifest**: No `pyproject.toml`, `setup.cfg`, `setup.py`, or `requirements.txt` exists. The architect's spec should include a decision on packaging and dependency management (e.g., `uv` + `pyproject.toml` is a modern choice consistent with the `.gitignore`).

3. **Obsidian CLI references in spec**: The spec mentions an Obsidian CLI (`plugins filter=core`, `plugin:enable id=<id>`) — this should be verified during web research. Obsidian does not officially support a standalone CLI in the traditional sense; this may refer to the Obsidian URI scheme or a community tool. The spec's Obsidian CLI links (`https://obsidian.md/help/cli`) should be validated.

4. **LLM integration is central**: The OpenAI API is a core dependency, not an optional one. This means LLM security review will be triggered in the MAPS workflow.

5. **First-run UX**: The spec proposes interactive first-run setup to capture the target codebase path. This implies the CLI needs a setup/init subcommand or interactive prompting on first `ingest`.

6. **Vault-relative execution**: The spec proposes running the tool from the root of the Obsidian vault, so the vault path is implicit (CWD). This is a meaningful architectural constraint for the CLI design.
