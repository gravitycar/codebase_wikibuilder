"""Obsidian CLI integration helpers for Codebase Wiki Builder.

Provides best-effort subprocess invocation of the Obsidian CLI to enable
the Search core plugin in the active vault. All failures are handled
gracefully — this module never raises and never blocks other operations.

FR-7: Obsidian Plugin Management (optional / exploratory).
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone

_logger = logging.getLogger("codebase_wiki_builder.obsidian_cli")

OBSIDIAN_TIMEOUT: int = 5  # seconds; spec mandates 5-second timeout


def _utc_now() -> str:
    """Return a UTC timestamp string formatted for log.md entries."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def try_enable_search_plugin(log_fn: Callable[[str], None]) -> None:
    """Attempt to enable the Obsidian Search core plugin via the Obsidian CLI.

    Degrades gracefully if Obsidian is not installed, not running, or does not
    respond within 5 seconds. All failures are logged as warnings only.
    This function never raises and never blocks other operations.

    Parameters
    ----------
    log_fn:
        A callable that appends one string entry to log.md
        (i.e., ``append_log_md`` partially applied with ``vault_root``,
        or any equivalent callable). This abstraction keeps the function
        decoupled from the filesystem layout and trivially testable.

    Notes
    -----
    Typical call site in cli.py (inside the ``ingest`` command body)::

        from functools import partial
        from codebase_wiki_builder.logging_setup import append_log_md
        from codebase_wiki_builder.obsidian_cli import try_enable_search_plugin

        log_fn = partial(append_log_md, vault_root)
        try_enable_search_plugin(log_fn)

    The success path logs at INFO level only and does NOT call ``log_fn`` —
    a successful plugin enable is a routine operational detail that does not
    need to appear in the human-readable log.md. Only warnings go to log.md
    for this optional feature.
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
