# Implementation Plan: Summarization and Summary File Writer (Ingest Phase 2 Core)

## Spec Context

This plan implements the core of Ingest Phase 2: for every source file classified as new or modified by the Phase 1 scanner, `summarize_file()` constructs and sends the LLM prompt, parses the structured response (description, explicit references, dynamic references), validates returned file references against the real codebase file tree, and assembles the final summary string. `write_summary()` writes that string to the correct vault path, creating parent directories as needed. Together these two functions constitute the primary output mechanism of the `ingest` command — every vault summary file originates here.

Catalog item: 6 — Summarization and Summary File Writer (Ingest Phase 2 Core)
Specification section: FR-3.4 (summarization prompt, inter-request delay), FR-3.5 (summary file format: title, description, `## References` with `(inferred)` annotation, MD5 footer `<!-- md5: ... -->`)
Acceptance criteria addressed: FR-3.4 summarization prompt instructs LLM to produce description + explicit/dynamic references; FR-3.5 summary format (H1 title → description/class sections → `## References` → MD5 footer); `(inferred)` annotation on dynamic references; reference validation against real file tree; `write_summary()` creates parent directories and writes the file.

## Dependencies

- **Blocked by**: Item 3 (LLM Client) — needs `LLMClient` and `LLMError`
- **Blocked by**: Item 4 (Vault File Utilities) — needs `vault_path_for_source()`, `compute_md5()`, `md5_footer()`, `wikilink()`
- **Blocked by**: Item 5 (Scanner) — `ChangeSet` is the input; `scan_codebase()` must complete (Phase 1) before `summarize_file()` is called for any file
- **Blocks**: Item 7 (Deletion Handling) — Phase 2 summary writes must complete before deletions and backlink cleanup run
- **Uses**: `pathlib` (stdlib), `re` (stdlib), `json` (stdlib), `logging` (stdlib); `LLMClient` from `llm_client.py`; `vault_path_for_source()`, `compute_md5()`, `md5_footer()`, `wikilink()` from `vault.py`; `WikiConfig` from `config.py`

## File Changes

### New Files

- `codebase_wiki_builder/summarizer.py` — `summarize_file()`, `write_summary()`, prompt construction helpers, LLM response parsing, reference validation

### Modified Files

- None

---

## Implementation Details

### `summarizer.py`

**File**: `codebase_wiki_builder/summarizer.py`

**Exports**:
- `summarize_file(path: Path, llm_client: LLMClient, config: WikiConfig, vault_root: Path, logger: logging.Logger) -> str` — builds prompt, calls LLM, validates references, assembles summary string
- `write_summary(vault_summary_path: Path, summary_str: str) -> None` — writes the summary string to disk, creating parent directories

---

### LLM Response Schema

The LLM is prompted to return a JSON object (not free-form markdown) so that structured fields — description, explicit references, dynamic references — can be parsed and validated separately. After parsing, the summary markdown is assembled locally.

The JSON schema expected from the LLM:

```json
{
  "description": "<markdown string: description of the file, or per-class/module sections>",
  "explicit_references": ["relative/path/to/file.py", "..."],
  "dynamic_references": [
    {"path": "relative/path/to/plugin.py", "reason": "why this reference is inferred"},
    "..."
  ]
}
```

Field definitions:
- `description`: A markdown-formatted string. If the file defines classes or modules, this contains sub-sections (`### ClassName`) with brief summaries of properties and methods. Otherwise, prose description of what the file does.
- `explicit_references`: List of strings. Paths to files that statically import/require/include the file being summarized (e.g., files that `import` from it). Paths are relative to the codebase root.
- `dynamic_references`: List of objects. Each has `path` (relative to codebase root) and `reason` (brief explanation of why the reference is suspected). These are runtime/dynamic patterns — dynamic imports, plugin loaders, string-based path construction, etc.

If the LLM returns malformed JSON or a response that does not match this schema, the fallback is to use the raw response text as the description with empty reference lists (logged at WARNING level). This ensures a summary is still written even if structured parsing fails.

---

### Prompt Construction

The prompt is constructed by `_build_prompt()`:

```python
def _build_prompt(
    source_file: Path,
    codebase_root: Path,
    file_content: str,
) -> str:
```

The prompt instructs the LLM to:
1. Summarize the file's purpose. If it defines classes or modules, produce a section per class/module with properties and methods summarized. Otherwise, write plain prose.
2. List files that **explicitly** reference the file being summarized (imports, requires, includes) — these are files in the codebase that import/use THIS file, not files that THIS file imports.
3. List files that **dynamically** reference it (runtime loading patterns, string-based path construction), with a reason for each.
4. Return a JSON object matching the schema above, enclosed in a ```json ... ``` fence.

**Key prompt wording** (exact text to ensure the LLM understands directionality):

```python
# PROMPT_TEMPLATE is kept here as documentation only — do NOT use with .format() at runtime.
# Curly braces in file_content (untrusted) would corrupt the template or raise KeyError.
# Use _build_prompt() which constructs the prompt via an f-string.
PROMPT_TEMPLATE = """\
You are analyzing a source file to produce a wiki summary. Your response MUST be a JSON object
enclosed in a ```json ... ``` code fence. Do not include any text outside the fence.

## File to summarize
Path (relative to codebase root): {relative_path}

## File contents
```
{file_content}
```

## Instructions

Produce a JSON object with exactly these fields:

1. "description": A markdown string summarizing what this file does.
   - If the file defines one or more classes or modules, produce a sub-section (### ClassName)
     for each, briefly listing its key properties and methods.
   - Otherwise, write 1-3 paragraphs of plain prose describing the file's purpose.

2. "explicit_references": A JSON array of relative file paths (strings).
   List files IN THE CODEBASE that explicitly import, require, or include THIS file
   (not files that this file imports from). Use paths relative to the codebase root.
   If none, return an empty array [].

3. "dynamic_references": A JSON array of objects, each with "path" and "reason" fields.
   List files IN THE CODEBASE that likely reference THIS file at runtime through dynamic
   patterns (e.g., dynamic imports, plugin loaders, string-based path construction).
   If none, return an empty array [].

Return ONLY the JSON object inside a ```json ... ``` fence. No other text.
"""
```

Note the directionality emphasis: "files IN THE CODEBASE that explicitly import... THIS file". This is a subtle but critical point — the References section captures inbound references (who uses this file), not outbound imports (what this file depends on). The prompt must make this clear.

Implementation:

```python
def _build_prompt(
    source_file: Path,
    codebase_root: Path,
    file_content: str,
) -> str:
    relative_path = source_file.relative_to(codebase_root).as_posix()
    # Use an f-string rather than PROMPT_TEMPLATE.format() so that curly braces
    # in untrusted content (relative_path, file_content) cannot corrupt the prompt
    # or raise KeyError at the Python layer.
    return (
        "You are analyzing a source file to produce a wiki summary. Your response MUST be a JSON object\n"
        "enclosed in a ```json ... ``` code fence. Do not include any text outside the fence.\n"
        "\n"
        "## File to summarize\n"
        f"Path (relative to codebase root): {relative_path}\n"
        "\n"
        "## File contents\n"
        "```\n"
        f"{file_content}\n"
        "```\n"
        "\n"
        "## Instructions\n"
        "\n"
        'Produce a JSON object with exactly these fields:\n'
        "\n"
        '1. "description": A markdown string summarizing what this file does.\n'
        "   - If the file defines one or more classes or modules, produce a sub-section (### ClassName)\n"
        "     for each, briefly listing its key properties and methods.\n"
        "   - Otherwise, write 1-3 paragraphs of plain prose describing the file's purpose.\n"
        "\n"
        '2. "explicit_references": A JSON array of relative file paths (strings).\n'
        "   List files IN THE CODEBASE that explicitly import, require, or include THIS file\n"
        "   (not files that this file imports from). Use paths relative to the codebase root.\n"
        "   If none, return an empty array [].\n"
        "\n"
        '3. "dynamic_references": A JSON array of objects, each with "path" and "reason" fields.\n'
        "   List files IN THE CODEBASE that likely reference THIS file at runtime through dynamic\n"
        "   patterns (e.g., dynamic imports, plugin loaders, string-based path construction).\n"
        "   If none, return an empty array [].\n"
        "\n"
        "Return ONLY the JSON object inside a ```json ... ``` fence. No other text.\n"
    )
```

---

### LLM Response Parsing

`_parse_llm_response()` extracts the JSON from the LLM's output and validates its structure.

```python
from __future__ import annotations
import json
import re

_JSON_FENCE_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)


def _parse_llm_response(raw: str, logger: logging.Logger) -> dict:
    """Extract and parse the JSON object from the LLM response.

    Returns a dict with keys: description (str), explicit_references (list[str]),
    dynamic_references (list[dict with 'path' and 'reason']).
    Falls back to a dict with the raw text as description and empty lists on any
    parse failure.
    """
    m = _JSON_FENCE_RE.search(raw)
    if not m:
        logger.warning("LLM response contained no ```json``` fence; using raw text as description")
        return {"description": raw.strip(), "explicit_references": [], "dynamic_references": []}

    json_text = m.group(1)
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as exc:
        logger.warning("LLM response JSON parse failed (%s); using raw text as description", exc)
        return {"description": raw.strip(), "explicit_references": [], "dynamic_references": []}

    # Validate and coerce fields
    description = str(data.get("description", "")).strip()
    if not description:
        description = raw.strip()
        logger.warning("LLM response 'description' field is empty; using raw text")

    explicit_refs = data.get("explicit_references", [])
    if not isinstance(explicit_refs, list):
        logger.warning("'explicit_references' is not a list; ignoring")
        explicit_refs = []
    explicit_refs = [r for r in explicit_refs if isinstance(r, str) and r.strip()]

    dynamic_refs = data.get("dynamic_references", [])
    if not isinstance(dynamic_refs, list):
        logger.warning("'dynamic_references' is not a list; ignoring")
        dynamic_refs = []
    # Keep only objects with 'path' and 'reason' string fields
    validated_dynamic = []
    for item in dynamic_refs:
        if isinstance(item, dict) and isinstance(item.get("path"), str) and isinstance(item.get("reason"), str):
            if item["path"].strip():
                validated_dynamic.append({"path": item["path"].strip(), "reason": item["reason"].strip()})
        else:
            logger.warning("Skipping malformed dynamic_references entry: %r", item)

    return {
        "description": description,
        "explicit_references": explicit_refs,
        "dynamic_references": validated_dynamic,
    }
```

---

### Reference Validation

`_validate_references()` takes the parsed reference lists and cross-references them against the real codebase file tree. Paths that do not correspond to a real existing file are discarded. Returns two lists: validated explicit reference paths (as `Path` objects) and validated dynamic reference entries (as dicts with `path: Path` and `reason: str`).

```python
def _validate_references(
    explicit_refs: list[str],
    dynamic_refs: list[dict],
    codebase_root: Path,
    logger: logging.Logger,
) -> tuple[list[Path], list[dict]]:
    """Resolve reference paths against the real codebase. Discard non-existent paths.

    Returns:
        valid_explicit: list[Path] — absolute paths of valid explicit references
        valid_dynamic: list[dict] — dicts with keys 'path' (Path) and 'reason' (str)
    """
    valid_explicit: list[Path] = []
    for ref_str in explicit_refs:
        ref_path = codebase_root / ref_str.lstrip("/")
        if ref_path.exists() and ref_path.is_file():
            valid_explicit.append(ref_path)
        else:
            logger.debug("Discarding explicit reference (not found): %s", ref_str)

    valid_dynamic: list[dict] = []
    for item in dynamic_refs:
        ref_path = codebase_root / item["path"].lstrip("/")
        if ref_path.exists() and ref_path.is_file():
            valid_dynamic.append({"path": ref_path, "reason": item["reason"]})
        else:
            logger.debug("Discarding dynamic reference (not found): %s", item["path"])

    return valid_explicit, valid_dynamic
```

Reference paths from the LLM are treated as relative to the codebase root. The `lstrip("/")` guard prevents absolute-path injection (in case the LLM returns a leading slash).

---

### Summary String Assembly

`_assemble_summary()` builds the final markdown string from the validated parts.

```python
def _assemble_summary(
    source_file: Path,
    codebase_root: Path,
    vault_root: Path,
    description: str,
    valid_explicit: list[Path],
    valid_dynamic: list[dict],
    md5_hex: str,
) -> str:
    """Assemble the complete summary markdown string.

    Format:
        # <relative path from codebase root>

        <description>

        ## References
        - [[vault/relative/path/to/explicit/file]]
        - [[vault/relative/path/to/dynamic/file]] (inferred)
        - ...

        <!-- md5: <hexdigest> -->
    """
    relative_path = source_file.relative_to(codebase_root).as_posix()
    lines: list[str] = []

    # H1 title
    lines.append(f"# {relative_path}")
    lines.append("")

    # Description
    lines.append(description.strip())
    lines.append("")

    # ## References section (always emitted, even if empty)
    lines.append("## References")
    for explicit_path in valid_explicit:
        vault_summary = vault_path_for_source(explicit_path, codebase_root, vault_root)
        link = wikilink(vault_summary, vault_root)
        lines.append(f"- {link}")
    for dyn_item in valid_dynamic:
        vault_summary = vault_path_for_source(dyn_item["path"], codebase_root, vault_root)
        link = wikilink(vault_summary, vault_root)
        lines.append(f"- {link} (inferred)")

    # MD5 footer
    lines.append("")
    lines.append(md5_footer(md5_hex))

    return "\n".join(lines)
```

The `## References` section is always emitted (spec mandates it as part of the format) even if both reference lists are empty. An empty References section is valid — it simply means no files were found that reference this one.

The wikilink format uses `vault_path_for_source()` to convert the validated source path to the expected vault summary path, then `wikilink()` to format it as `[[relative/path/to/file]]` (without `.md` extension, per Obsidian convention).

---

### `summarize_file()` — Main Entry Point

```python
def summarize_file(
    path: Path,
    llm_client: LLMClient,
    config: WikiConfig,
    vault_root: Path,
    logger: logging.Logger,
) -> str:
    """Summarize a single source file.

    Reads file content, builds prompt, calls LLM, parses structured response,
    validates references against the real codebase, assembles and returns the
    final summary markdown string.

    The returned string is ready to be passed to write_summary().

    Raises:
        LLMError: if the LLM API call fails (non-retriable or all retries exhausted).
        OSError: if the source file cannot be read.
    """
    codebase_root = Path(config.codebase_path)

    # Read file content
    file_content = path.read_text(encoding="utf-8", errors="replace")

    # Compute MD5 of the source file (used in footer)
    md5_hex = compute_md5(path)

    # Build prompt
    prompt = _build_prompt(path, codebase_root, file_content)

    # Call LLM (LLMClient enforces inter-request delay internally)
    logger.debug("Summarizing: %s", path)
    raw_response = llm_client.complete(prompt)

    # Parse structured response
    parsed = _parse_llm_response(raw_response, logger)

    # Validate references against real codebase
    valid_explicit, valid_dynamic = _validate_references(
        parsed["explicit_references"],
        parsed["dynamic_references"],
        codebase_root,
        logger,
    )

    # Assemble final summary markdown
    summary_str = _assemble_summary(
        path,
        codebase_root,
        vault_root,
        parsed["description"],
        valid_explicit,
        valid_dynamic,
        md5_hex,
    )

    logger.debug(
        "Summary assembled for %s: %d explicit refs, %d dynamic refs",
        path.name,
        len(valid_explicit),
        len(valid_dynamic),
    )
    return summary_str
```

`LLMError` propagates to the caller (the CLI ingest command, item 9), which handles exit code 1. `OSError` on file read also propagates — the CLI logs it and counts it as a failed file (contributing to exit code 2).

---

### `write_summary()` — Write to Vault

```python
def write_summary(vault_summary_path: Path, summary_str: str) -> None:
    """Write a summary string to the vault at the given path.

    Creates parent directories if they do not exist.
    Overwrites any existing summary at that path (correct for re-summarization).

    Raises:
        OSError: if the file cannot be written (permission error, disk full, etc.)
    """
    vault_summary_path.parent.mkdir(parents=True, exist_ok=True)
    vault_summary_path.write_text(summary_str, encoding="utf-8")
```

`mkdir(parents=True, exist_ok=True)` handles both the initial creation of nested vault directories and the idempotent case where they already exist. `write_text()` performs an atomic-ish overwrite on most operating systems — on failure (disk full, permission denied), `OSError` propagates to the caller.

The caller (ingest CLI, item 9) is responsible for computing `vault_summary_path` using `vault_path_for_source()` from `vault.py`. `write_summary()` itself is intentionally dumb about path computation — it just writes.

---

### Complete Module Skeleton

```python
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from codebase_wiki_builder.config import WikiConfig
from codebase_wiki_builder.llm_client import LLMClient
from codebase_wiki_builder.vault import (
    compute_md5,
    md5_footer,
    vault_path_for_source,
    wikilink,
)

logger = logging.getLogger(__name__)

_JSON_FENCE_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)

PROMPT_TEMPLATE: str = ...  # documentation only — NOT used with .format() at runtime


def _build_prompt(source_file: Path, codebase_root: Path, file_content: str) -> str: ...
def _parse_llm_response(raw: str, logger: logging.Logger) -> dict: ...
def _validate_references(
    explicit_refs: list[str],
    dynamic_refs: list[dict],
    codebase_root: Path,
    logger: logging.Logger,
) -> tuple[list[Path], list[dict]]: ...
def _assemble_summary(
    source_file: Path,
    codebase_root: Path,
    vault_root: Path,
    description: str,
    valid_explicit: list[Path],
    valid_dynamic: list[dict],
    md5_hex: str,
) -> str: ...
def summarize_file(
    path: Path,
    llm_client: LLMClient,
    config: WikiConfig,
    vault_root: Path,
    logger: logging.Logger,
) -> str: ...
def write_summary(vault_summary_path: Path, summary_str: str) -> None: ...
```

---

## Error Handling

| Condition | Location | Behavior |
|-----------|----------|----------|
| `path.read_text()` raises `OSError` (unreadable file) | `summarize_file()` | Propagates to caller (CLI counts as failed file; contributes to exit code 2) |
| `compute_md5()` raises `OSError` | `summarize_file()` | Propagates to caller |
| LLM API rate-limit (≤5 attempts) | `llm_client.complete()` | `LLMClient` retries with backoff (handled transparently, logged at WARNING) |
| LLM API rate-limit (all 5 attempts exhausted) | `llm_client.complete()` | `LLMError` raised; propagates to CLI; `sys.exit(1)` |
| LLM non-retriable API error | `llm_client.complete()` | `LLMError` raised; propagates to CLI; `sys.exit(1)` |
| LLM response missing ```json``` fence | `_parse_llm_response()` | WARNING logged; raw text used as description; empty reference lists |
| LLM response JSON parse failure | `_parse_llm_response()` | WARNING logged; raw text used as description; empty reference lists |
| LLM returns malformed `explicit_references` (not a list) | `_parse_llm_response()` | WARNING logged; field ignored; empty list used |
| LLM returns malformed `dynamic_references` entries | `_parse_llm_response()` | WARNING per bad entry; bad entries skipped; valid ones kept |
| Reference path does not exist in codebase | `_validate_references()` | DEBUG logged; path discarded silently |
| `vault_summary_path.parent.mkdir()` fails (`OSError`) | `write_summary()` | Propagates to caller |
| `vault_summary_path.write_text()` fails (`OSError`) | `write_summary()` | Propagates to caller |

---

## Unit Test Specifications

**File**: `tests/test_summarizer.py`

All tests use `tmp_path` for both a fake codebase directory and a fake vault root. LLM calls are mocked via `unittest.mock.MagicMock` on `llm_client.complete`.

---

### `_build_prompt()`

| Case | Input | Expected | Why |
|------|-------|----------|-----|
| Basic file | `source_file=cb/src/auth.py`, content=`"def auth(): pass"` | Prompt contains `"src/auth.py"` (relative path) and the file content | Path is relative to codebase root |
| Root-level file | `source_file=cb/main.py` | Prompt contains `"main.py"` (no prefix) | Root-level relative path |
| Prompt structure | Any file | Contains `"explicit_references"` and `"dynamic_references"` as JSON field names | Schema documented in prompt |
| Directionality | Any file | Prompt text includes "files IN THE CODEBASE that explicitly import... THIS file" (or equivalent) | Ensures LLM understands inbound direction |

---

### `_parse_llm_response()` — happy path

| Case | Input | Expected | Why |
|------|-------|----------|-----|
| Valid JSON fence | Full JSON object in ` ```json ... ``` ` | Returns dict with `description`, `explicit_references`, `dynamic_references` | Normal LLM response |
| Empty reference lists | JSON with `[]` for both ref fields | Returns empty lists | No references case |
| Dynamic ref with path and reason | JSON `dynamic_references=[{"path":"x.py","reason":"plugin"}]` | Returns validated entry | Well-formed dynamic ref |
| Leading/trailing whitespace in paths | `explicit_references=["  src/foo.py  "]` | Path is stripped to `"src/foo.py"` | Normalize whitespace |

---

### `_parse_llm_response()` — fallback cases

| Case | Input | Expected | Why |
|------|-------|----------|-----|
| No JSON fence | Plain text response | `description` = raw text; refs = empty lists; WARNING logged | Graceful fallback |
| Malformed JSON in fence | ```` ```json {broken ``` ```` | `description` = raw text; refs = empty; WARNING logged | JSON parse error |
| `explicit_references` not a list | `"explicit_references": "not a list"` | Ignored; empty list used; WARNING logged | Type coercion |
| `dynamic_references` entry missing `path` | `[{"reason": "only reason"}]` | Entry skipped; WARNING logged | Malformed entry |
| Empty `description` | `"description": ""` | Falls back to raw text; WARNING logged | Empty description |

---

### `_validate_references()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Valid explicit path | `codebase/src/auth.py` exists; explicit_refs=`["src/auth.py"]` | Returns `[Path("codebase/src/auth.py")]` in valid_explicit | File exists |
| Non-existent explicit path | `explicit_refs=["src/gone.py"]`; file not on disk | Not in valid_explicit; DEBUG logged | Discarded |
| Valid dynamic path | `codebase/plugins/loader.py` exists; dynamic ref with that path | Returns entry with `path=Path("...")` and `reason` string | Dynamic ref resolved |
| Non-existent dynamic path | `dynamic_refs=[{"path":"gone.py","reason":"x"}]`; file gone | Not in valid_dynamic; DEBUG logged | Discarded |
| Leading slash stripped | `explicit_refs=["/src/auth.py"]`; `codebase/src/auth.py` exists | Resolved correctly (leading `/` stripped) | LLM path normalization |
| Directory path rejected | `explicit_refs=["src/"]`; `codebase/src/` is a directory | Not in valid_explicit (`.is_file()` check fails) | Dirs are not files |

---

### `_assemble_summary()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| H1 title | `source_file=cb/src/auth.py` | First line is `# src/auth.py` | FR-3.5: title is relative path |
| Description included | `description="Handles auth."` | Description block present in output | FR-3.5 |
| `## References` always present | No references | Output still contains `## References` heading | FR-3.5: section always emitted |
| Explicit ref formatted | Valid explicit `src/login.py` | Line `- [[src/login.py]]` in References (no `(inferred)`) | FR-3.5: wikilink format |
| Dynamic ref annotated | Valid dynamic `src/plugins/loader.py` | Line `- [[src/plugins/loader.py]] (inferred)` | FR-3.5: `(inferred)` annotation |
| MD5 footer as last line | Any content | Last line matches `<!-- md5: [a-f0-9]{32} -->` | FR-3.5: MD5 footer |
| Correct MD5 value | File with known content | Footer hexdigest matches `hashlib.md5(content).hexdigest()` | FR-3.3 + FR-3.5 |

---

### `summarize_file()` — integration (mock LLM)

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Happy path | Source file exists; LLM returns valid JSON; all refs exist | Returns string with H1, description, References, MD5 footer | End-to-end |
| LLM returns no fence | `llm_client.complete` returns plain text | Returns summary with description = raw text; empty References | Fallback path |
| References validated | LLM returns 2 explicit refs; one file exists, one doesn't | Only the existing file appears in References | Validation filters non-existent |
| Dynamic ref annotated | LLM returns one dynamic ref that exists | Summary line contains `(inferred)` | FR-3.5 annotation |
| `LLMError` propagates | `llm_client.complete` raises `LLMError` | `LLMError` raised from `summarize_file()` | Caller handles exit |
| `OSError` on read propagates | Source file unreadable | `OSError` raised | Caller counts as failed |
| MD5 computed from source | Source file content `b"hello"` | Footer hash = `md5(b"hello").hexdigest()` | FR-3.3: hash of source file |

---

### `write_summary()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Creates file | Fresh vault dir; call `write_summary` | File exists at the path | Basic write |
| Creates parent dirs | `vault/src/auth/` does not exist | Directories created; file written | Deep vault mirroring |
| Overwrites existing | Summary already exists; call again with new content | File has new content | Re-summarization |
| Content preserved | `summary_str = "# foo\n\n...\n<!-- md5: abc -->"` | File content equals that string | No modification |
| `OSError` propagates | Path in read-only directory | `OSError` raised | Caller handles |

---

### Key Scenario: Full summary round-trip

**Setup**: Create a fake codebase with two files:
- `codebase/src/auth.py` — content `"def authenticate(): pass"`
- `codebase/src/login.py` — exists on disk (so reference validation succeeds)

Mock `llm_client.complete` to return:
```json
{
  "description": "Handles authentication logic.",
  "explicit_references": ["src/login.py"],
  "dynamic_references": [{"path": "src/plugins/dynamic.py", "reason": "loaded at runtime"}]
}
```
(`src/plugins/dynamic.py` does NOT exist in the fake codebase — should be discarded.)

**Action**: Call `summarize_file(codebase/src/auth.py, llm_client, config, vault_root, logger)`.

**Expected**:
- Return value starts with `# src/auth.py\n`
- Contains `Handles authentication logic.`
- Contains `## References`
- Contains `- [[src/login.py]]` (explicit, not annotated)
- Does NOT contain `dynamic.py` (validated out)
- Last line matches `<!-- md5: [a-f0-9]{32} -->`

```python
def test_full_summary_round_trip(tmp_path):
    import hashlib, logging
    from unittest.mock import MagicMock
    from codebase_wiki_builder.summarizer import summarize_file
    from codebase_wiki_builder.config import WikiConfig

    codebase = tmp_path / "codebase"
    vault = tmp_path / "vault"
    codebase.mkdir()
    (codebase / "src").mkdir()
    vault.mkdir()

    auth_content = "def authenticate(): pass"
    auth_file = codebase / "src" / "auth.py"
    auth_file.write_text(auth_content)

    login_file = codebase / "src" / "login.py"
    login_file.write_text("# login")

    llm_response = '''{
      "description": "Handles authentication logic.",
      "explicit_references": ["src/login.py"],
      "dynamic_references": [{"path": "src/plugins/dynamic.py", "reason": "loaded at runtime"}]
    }'''

    mock_client = MagicMock()
    mock_client.complete.return_value = f"```json\n{llm_response}\n```"

    config = WikiConfig(codebase_path=str(codebase))
    logger = logging.getLogger("test")

    summary = summarize_file(auth_file, mock_client, config, vault, logger)

    assert summary.startswith("# src/auth.py\n")
    assert "Handles authentication logic." in summary
    assert "## References" in summary
    assert "[[src/login.py]]" in summary
    assert "(inferred)" not in summary  # explicit ref, not inferred
    assert "dynamic.py" not in summary  # discarded (doesn't exist)
    last_line = summary.strip().splitlines()[-1]
    expected_md5 = hashlib.md5(auth_content.encode()).hexdigest()
    assert last_line == f"<!-- md5: {expected_md5} -->"
```

---

### Key Scenario: `write_summary()` creates nested directories

```python
def test_write_summary_creates_dirs(tmp_path):
    from codebase_wiki_builder.summarizer import write_summary

    vault_path = tmp_path / "vault" / "src" / "auth" / "login.py.md"
    summary = "# src/auth/login.py\n\nSome content.\n\n## References\n\n<!-- md5: abc -->"

    write_summary(vault_path, summary)

    assert vault_path.exists()
    assert vault_path.read_text(encoding="utf-8") == summary
```

---

## Notes

- **Inbound reference direction**: The prompt asks for files that reference THIS file, not files that this file imports. This is the correct semantic for the `## References` section — it shows who depends on this file (backlinks), not what this file depends on. The prompt wording must emphasize this clearly to avoid LLM confusion.

- **JSON-in-fence response format**: Requiring the LLM to return JSON inside a ` ```json ``` ` code fence rather than free-form markdown ensures that description text (which may itself contain markdown headings, lists, etc.) does not interfere with response parsing. The regex `_JSON_FENCE_RE` extracts just the JSON block.

- **Fallback to raw text on parse failure**: If the LLM returns an unparseable response, `summarize_file()` does NOT raise an error — it logs a WARNING and produces a best-effort summary using the raw text as the description with empty References. This is the spec's implicit requirement: the application should survive transient LLM quirks without losing vault state (spec Reliability NFR). A partial summary is better than no summary for maintaining vault completeness.

- **MD5 is computed from source before LLM call**: `compute_md5()` is called at the start of `summarize_file()`, before `llm_client.complete()`. This ensures the MD5 footer reflects the file content that was actually sent to the LLM, even if the file were to change during a long LLM call (unlikely but theoretically possible).

- **`write_summary()` is call-site agnostic**: It takes a fully-computed `vault_summary_path` (absolute `Path`) from the caller. The caller (ingest CLI, item 9) uses `vault_path_for_source()` to compute this path. Keeping path computation out of `write_summary()` follows single-responsibility and makes the function trivially testable.

- **`errors="replace"` on file read**: `path.read_text(encoding="utf-8", errors="replace")` is used rather than strict UTF-8 decoding. Source files that are valid UTF-8 will be read cleanly. Files with isolated encoding errors (e.g., a binary blob accidentally named `.py`) will have replacement characters in the content sent to the LLM rather than causing an `OSError` — the LLM will still produce a summary, likely noting that the file is partially binary. This matches the spirit of "survive transient issues without crashing".

- **Inter-request delay is enforced in `LLMClient`**: `summarize_file()` does not manage timing — it calls `llm_client.complete()` and the `LLMClient` handles inter-request delay internally. This keeps `summarizer.py` clean and avoids duplicating delay logic.

- **`## References` always emitted**: Even if both reference lists are empty, the `## References` heading appears in the summary. This ensures consistent format for downstream parsing (staleness detection, deletion cleanup) and matches the spec's mandated section order.

- **Dynamic reference `(inferred)` annotation**: The annotation is appended to the wikilink on the same line: `- [[src/plugins/loader.py]] (inferred)`. This is the format specified in FR-3.5 and the spec example. Explicit references have no annotation.

- **Reference path normalization**: `lstrip("/")` on LLM-returned paths prevents absolute-path injection (e.g., `/etc/passwd` becoming `codebase_root / "/etc/passwd"`, which would resolve to `/etc/passwd` via `Path.__truediv__`). Always treat LLM-returned paths as relative to the codebase root.

- **Caller computes `vault_summary_path` for `write_summary()`**: The ingest CLI (item 9) orchestrates the loop: for each file in `change_set.new_files + change_set.modified_files`, it calls `summarize_file()` then `write_summary(vault_path_for_source(file, codebase_root, vault_root), summary_str)`. The summarizer module itself does not need to know the vault path — that is a vault.py concern.
