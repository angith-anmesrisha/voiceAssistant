from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from loguru import logger

from hitodama.app.logging_setup import configure_logging
from hitodama.app.single_instance import SingleInstance


def test_logging_writes_rotating_file(tmp_path: Path) -> None:
    log_file = configure_logging("INFO", tmp_path)
    logger.info("hello from test")
    logger.complete()
    logger.remove()
    assert "hello from test" in log_file.read_text(encoding="utf-8")


def test_second_instance_is_refused() -> None:
    first = SingleInstance("HitodamaTest")
    second = SingleInstance("HitodamaTest")
    assert first.acquire()
    try:
        assert not second.acquire()
    finally:
        first.release()
    assert second.acquire()
    second.release()


def test_app_starts_and_exits_cleanly(isolated_env: Path) -> None:
    env = {**os.environ, "QT_QPA_PLATFORM": os.environ.get("QT_QPA_PLATFORM", "offscreen")}
    result = subprocess.run(
        [sys.executable, "-m", "hitodama", "--exit-after", "1.5"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    log = (isolated_env / "logs" / "hitodama.log").read_text(encoding="utf-8")
    assert "running" in log
    assert "exited cleanly" in log
