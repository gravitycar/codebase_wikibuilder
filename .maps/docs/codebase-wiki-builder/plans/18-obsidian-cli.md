# Implementation Plan: Obsidian CLI Integration (Optional)

## Spec Context

This plan implements the optional, best-effort Obsidian CLI integration described in FR-7. It attempts to enable the Obsidian Search core plugin by invoking the Obsidian CLI subprocess against the active vault (the current working directory). The feature is explicitly marked exploratory in the spec — no acceptance test depends on it, and the application must operate normally regardless of whether this step succeeds.

Catalog item: 18 — Obsidian CLI Integration (Optional / Exploratory)
Specification section: FR-7 (Obsidian Plugin Management), Technical Context (Obsidian CLI Integration)
Acceptance criteria addressed: FR-7 (best-effort invocation, 5-second timeout, graceful degradation, warning-only logging on failure, no blocking of other operations)

## Dependencies

- **Blocked by**: Item 1 (Project Scaffold) — package must exist; Item 4 (Vault Utilities and Logging Infrastructure) — uses `append_log_md` and the application logger from `logging_setup.py`
- **Blocks**: None (this is an independent optional leaf item)
- **Uses**: `subprocess` (stdlib), `logging` (stdlib); `append_log_md` from `codebase_wiki_builder.logging_setup`

## File Changes

### New Files

- `codebase_wiki_builder/obsidian_cli.py` — `try_enable_search_plugin(log_fn)`: subprocess invocation with 5-second timeout, warning-level error handling, graceful degradation

### Modified Files

- None. This module is called by the CLI entry point (item 9, `cli.py`), but no modification to `cli.py` is specified in this plan — the call site is already wired in item 9's scope. If item 9 has not yet been built, a note in its plan covers the integration point.

## Implementation Details

### `obsidian_cli.py`

**File**: `codebase_wiki_builder/obsidian_cli.py`

**Exports**:
- `try_enable_search_plugin(log_fn: Callable[[str], None]) -> None` — attempts to enable the Obsidian Search core plugin via the CLI; logs all failures as warnings via both `log_fn` and the module logger; never raises; never blocks

---

#### Background: Obsidian CLI Command Syntax

Per the spec's Technical Context section, the Obsidian CLI uses bare-word `key=value` parameter syntax. To enable the Search core plugin, the invocation is:

```
obsidian plugin:enable id=search
```

The CLI communicates with a running Obsidian instance via IPC. It requires:
1. The `obsidian` binary to be in `PATH`
2. Obsidian desktop to be running at the time of invocation

Neither condition can be guaranteed — graceful degradation on any failure is mandatory.

---

#### `OBSIDIAN_TIMEOUT` constant

```python
OBSIDIAN_TIMEOUT: int = 5  # seconds; spec mandates 5-second timeout
```

Module-level constant. Makes the timeout explicit and easy to locate.

---

#### `try_enable_search_plugin(log_fn)`

**Signature**:

```python
def try_enable_search_plugin(log_fn: Callable[[str], None]) -> None:
```

**Parameters**:
- `log_fn` — a callable that appends one string entry to `log.md` (i.e., `append_log_md` partially applied with `vault_root`, or any equivalent callable). Used so the function does not need to know the vault root directly and remains testable with a simple mock.

**Behavior**:

1. Attempt to run `obsidian plugin:enable id=search` as a subprocess.
2. Apply a 5-second wall-clock timeout to the subprocess call.
3. On any failure (binary not found, timeout, non-zero exit code, or any other exception), log a warning via both `_logger.warning(...)` and `log_fn(...)`. Never raise. Never call `sys.exit()`.
4. On success (process exits with code 0), log an info-level message confirming the plugin was enabled.

**Code Example**:

```python
import logging
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone

_logger = logging.getLogger("codebase_wiki_builder.obsidian_cli")

OBSIDIAN_TIMEOUT: int = 5  # seconds


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def try_enable_search_plugin(log_fn: Callable[[str], None]) -> None:
    """Attempt to enable the Obsidian Search core plugin via the Obsidian CLI.

    Degrades gracefully if Obsidian is not installed, not running, or does not
    respond within 5 seconds. All failures are logged as warnings only.
    This function never raises and never blocks other operations.
    """
    cmd = ["obsidian", "plugin:enable", "id=search"]
    try:
        result = subprocess.run(
            cmd,
            timeout=OBSIDIAN_TIMEOUT,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        msg = "Obsidian CLI not found in PATH; skipping search plugin activation."
        _logger.warning(msg)
        log_fn(f"{_utc_now()} | obsidian-cli | WARNING: {msg}")
        return
    except subprocess.TimeoutExpired:
        msg = (
            f"Obsidian CLI did not respond within {OBSIDIAN_TIMEOUT}s; "
            "skipping search plugin activation."
        )
        _logger.warning(msg)
        log_fn(f"{_utc_now()} | obsidian-cli | WARNING: {msg}")
        return
    except Exception as exc:  # noqa: BLE001
        msg = f"Obsidian CLI invocation failed unexpectedly: {exc}"
        _logger.warning(msg)
        log_fn(f"{_utc_now()} | obsidian-cli | WARNING: {msg}")
        return

    if result.returncode != 0:
        stderr_snippet = result.stderr.strip()[:200]
        msg = (
            f"Obsidian CLI exited with code {result.returncode}; "
            f"skipping search plugin activation. stderr: {stderr_snippet!r}"
        )
        _logger.warning(msg)
        log_fn(f"{_utc_now()} | obsidian-cli | WARNING: {msg}")
        return

    _logger.info("Obsidian Search core plugin enabled successfully.")
```

**Key design decisions**:

- `FileNotFoundError` is caught separately from the generic `Exception` catch because it is the expected error when Obsidian is not installed — it deserves a distinct, user-friendly message.
- `subprocess.TimeoutExpired` is caught separately and given its own message since the timeout is a spec-mandated constraint.
- The generic `except Exception` catch-all handles any other failure mode (e.g., `PermissionError`, `OSError` from subprocess spawning, unexpected library errors). The `# noqa: BLE001` comment suppresses Ruff's "blind exception" lint rule, which would otherwise flag `except Exception` — here the suppression is intentional and correct.
- `capture_output=True` prevents the Obsidian CLI's stdout/stderr from leaking to the terminal. The spec says this operation should be invisible to normal users unless something goes wrong.
- `text=True` decodes stdout/stderr as strings, which is more convenient for logging the `stderr_snippet`.
- The non-zero exit code branch captures only the first 200 characters of stderr to prevent unbounded log entries if Obsidian returns a very long error message.
- `log_fn` is called with a fully formatted entry string including a UTC timestamp and an `obsidian-cli` operation tag. This matches the general `log.md` entry pattern established by `logging_setup.py` (callers supply the full formatted string).
- The function does NOT call `append_log_md` directly (which would require knowing `vault_root`). Instead it accepts `log_fn` as a dependency-injected callable. Typical usage at the call site in `cli.py`:

```python
from functools import partial
from codebase_wiki_builder.logging_setup import append_log_md
from codebase_wiki_builder.obsidian_cli import try_enable_search_plugin

# In the CLI startup sequence:
log_fn = partial(append_log_md, vault_root)
try_enable_search_plugin(log_fn)
```

- The success path logs at `INFO` level (not `WARNING`) and does NOT call `log_fn` — a successful plugin enable is a routine operational detail that does not need to appear in the human-readable `log.md`. Only warnings go to `log.md` for this optional feature.

---

## Error Handling

| Condition | Behavior |
|-----------|----------|
| `obsidian` binary not in PATH | Catch `FileNotFoundError`; log warning to debug log and `log.md`; return |
| Obsidian not running / IPC timeout | Catch `subprocess.TimeoutExpired`; log warning to debug log and `log.md`; return |
| Any other subprocess failure | Catch `Exception`; log warning with exception message to debug log and `log.md`; return |
| Non-zero exit code from Obsidian CLI | Log warning with exit code and stderr snippet; return |
| Success (exit code 0) | Log info to debug log only; return |

No condition causes a raised exception or a non-zero exit code in the parent process. This is the core graceful-degradation invariant.

## Unit Test Specifications

**File**: `tests/test_obsidian_cli.py`

### `try_enable_search_plugin()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Obsidian not installed | Mock `subprocess.run` to raise `FileNotFoundError` | Function returns without raising; `log_fn` called once with WARNING message | Graceful degradation — not installed |
| CLI timeout | Mock `subprocess.run` to raise `subprocess.TimeoutExpired` | Function returns without raising; `log_fn` called once with timeout WARNING | Graceful degradation — not running |
| Unexpected exception | Mock `subprocess.run` to raise `OSError("permission denied")` | Function returns without raising; `log_fn` called once with exception message | Graceful degradation — any other error |
| Non-zero exit code | Mock `subprocess.run` to return `CompletedProcess(returncode=1, stderr="error msg")` | Function returns without raising; `log_fn` called once with exit-code WARNING | Graceful degradation — CLI error |
| Success | Mock `subprocess.run` to return `CompletedProcess(returncode=0, stdout="", stderr="")` | Function returns without raising; `log_fn` NOT called | Happy path — no log.md entry on success |
| Correct command invoked | Capture args passed to `subprocess.run` | First positional arg is `["obsidian", "plugin:enable", "id=search"]` | Correct CLI syntax |
| Timeout value used | Capture kwargs passed to `subprocess.run` | `timeout=5` | Spec mandates 5 seconds |
| stdout/stderr captured | Capture kwargs | `capture_output=True` | Terminal output suppressed |
| Log entry format on failure | Mock `FileNotFoundError`; inspect `log_fn` call arg | Entry contains `obsidian-cli`, `WARNING`, and UTC timestamp pattern | log.md format consistency |

---

### Key Scenario: FileNotFoundError (Obsidian not installed)

```python
from unittest.mock import MagicMock, patch

def test_obsidian_not_installed():
    from codebase_wiki_builder.obsidian_cli import try_enable_search_plugin

    log_fn = MagicMock()

    with patch("subprocess.run", side_effect=FileNotFoundError):
        try_enable_search_plugin(log_fn)  # must not raise

    log_fn.assert_called_once()
    call_arg = log_fn.call_args[0][0]
    assert "WARNING" in call_arg
    assert "obsidian-cli" in call_arg
    assert "not found" in call_arg.lower()
```

---

### Key Scenario: Timeout (Obsidian not running)

```python
import subprocess
from unittest.mock import MagicMock, patch

def test_obsidian_timeout():
    from codebase_wiki_builder.obsidian_cli import try_enable_search_plugin

    log_fn = MagicMock()

    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="obsidian", timeout=5),
    ):
        try_enable_search_plugin(log_fn)  # must not raise

    log_fn.assert_called_once()
    call_arg = log_fn.call_args[0][0]
    assert "WARNING" in call_arg
    assert "5" in call_arg  # timeout value mentioned
```

---

### Key Scenario: Non-zero exit code

```python
import subprocess
from unittest.mock import MagicMock, patch

def test_obsidian_nonzero_exit():
    from codebase_wiki_builder.obsidian_cli import try_enable_search_plugin

    log_fn = MagicMock()
    mock_result = subprocess.CompletedProcess(
        args=["obsidian", "plugin:enable", "id=search"],
        returncode=1,
        stdout="",
        stderr="vault not open",
    )

    with patch("subprocess.run", return_value=mock_result):
        try_enable_search_plugin(log_fn)  # must not raise

    log_fn.assert_called_once()
    call_arg = log_fn.call_args[0][0]
    assert "WARNING" in call_arg
    assert "1" in call_arg  # exit code
```

---

### Key Scenario: Success — no log_fn call

```python
import subprocess
from unittest.mock import MagicMock, patch

def test_obsidian_success_no_log_md_entry():
    from codebase_wiki_builder.obsidian_cli import try_enable_search_plugin

    log_fn = MagicMock()
    mock_result = subprocess.CompletedProcess(
        args=["obsidian", "plugin:enable", "id=search"],
        returncode=0,
        stdout="",
        stderr="",
    )

    with patch("subprocess.run", return_value=mock_result):
        try_enable_search_plugin(log_fn)  # must not raise

    log_fn.assert_not_called()  # success does not write to log.md
```

---

## Notes

- **Placement in the startup sequence**: `try_enable_search_plugin` should be called once at the start of any CLI command that modifies the vault (i.e., `ingest`). It is not needed before read-only commands (`query`, `analysis`, `lint`) since those do not change vault file structure. The exact call site is in `cli.py` (item 9), which is already built before item 18.
- **No vault root parameter**: The function does not take `vault_root` directly. The `log_fn` abstraction allows the function to remain decoupled from the filesystem layout and makes it trivial to test with a `MagicMock`.
- **`# noqa: BLE001` on bare `except Exception`**: Ruff rule BLE001 ("do not catch blind exception") is intentionally suppressed here. The spec explicitly requires catching all failures and degrading gracefully — a targeted exception list would risk missing future subprocess error types (e.g., `PermissionError` on restricted systems). The comment documents the intentionality.
- **Obsidian v1.12.4+ required**: The CLI was introduced in Obsidian v1.12.4 (February 2026). Users on earlier versions will see a `FileNotFoundError` (or a non-zero exit code with a suitable error message), which is handled gracefully. The requirement is documented in the spec's Dependencies section; `obsidian_cli.py` does not need to enforce or check the version.
- **No `shell=True`**: `subprocess.run` is called without `shell=True`. The command is passed as a list of strings, which avoids shell injection risks and is the correct approach for a known, fixed command.
- **`capture_output=True` vs. `stdout=PIPE, stderr=PIPE`**: `capture_output=True` is equivalent to `stdout=subprocess.PIPE, stderr=subprocess.PIPE` and is the preferred shorthand (Python 3.7+). Since the project targets Python 3.10+, this is safe.
- **No retry on failure**: The spec says failures are warnings and the operation is skipped. There is no retry logic — retrying would violate the "never blocks other operations" requirement.
- **Thread safety**: The function is stateless and has no shared mutable state. It is safe to call from any thread.
