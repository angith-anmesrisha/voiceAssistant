"""Loguru configuration: rotating file in the data dir, plus stderr in dev.

Never log audio, API keys, or dictated text.
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from jarvis.app.paths import logs_dir


def configure_logging(level: str = "INFO", directory: Path | None = None) -> Path:
    """Configure sinks and return the log file path."""
    directory = directory or logs_dir()
    log_file = directory / "jarvis.log"
    logger.remove()
    # sys.stderr is None in a windowed (no console) PyInstaller build.
    if sys.stderr is not None:
        logger.add(sys.stderr, level=level)
    logger.add(
        log_file,
        level=level,
        rotation="5 MB",
        retention=5,
        encoding="utf-8",
        enqueue=True,
        backtrace=False,
        diagnose=False,  # diagnose=True would dump local variables, which may hold secrets
    )
    return log_file
