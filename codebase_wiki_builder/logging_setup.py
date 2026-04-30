"""Logging infrastructure for Codebase Wiki Builder.

Provides two logging sinks:
- setup_logging(): creates a per-run debug log file under vault_root/logs/
- append_log_md(): append-only writer to vault_root/log.md
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = "logs"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

LOG_MD_FILENAME = "log.md"


def setup_logging(vault_root: Path) -> logging.Logger:
    """Create a per-run debug log file and configure the root logger.

    Creates vault_root/logs/YYYY-MM-DD_HH-MM-SS.log (UTC timestamp).
    Configures the root logger at DEBUG level with force=True to replace any
    existing handlers (important for test isolation).

    Returns the named logger "codebase_wiki_builder". Sub-modules should use
    logging.getLogger("codebase_wiki_builder.<module>") to get child loggers
    that automatically inherit this configuration.
    """
    logs_dir = vault_root / LOG_DIR
    logs_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    log_file = logs_dir / f"{timestamp}.log"

    logging.basicConfig(
        level=logging.DEBUG,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
        handlers=[logging.FileHandler(log_file, encoding="utf-8")],
        force=True,  # override any existing root logger handlers
    )

    logger = logging.getLogger("codebase_wiki_builder")
    logger.info("Logging initialized. Log file: %s", log_file)
    return logger


def append_log_md(vault_root: Path, entry: str) -> None:
    """Append one entry to vault_root/log.md.

    Never truncates or overwrites existing content. Opens in append mode on
    every call — no file handle is held open between calls.

    The caller is responsible for formatting the entry string (including any
    UTC timestamp prefix). A trailing newline is always written; any trailing
    newline already present in entry is stripped first to avoid double newlines.
    """
    log_path = vault_root / LOG_MD_FILENAME
    with log_path.open("a", encoding="utf-8") as f:
        f.write(entry.rstrip("\n") + "\n")
