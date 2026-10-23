from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis.app import keys


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point app data at a temp dir and hide any real API keys from every test."""
    data = tmp_path / "data"
    monkeypatch.setenv("JARVIS_DATA_DIR", str(data))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(keys, "read_user_environment", False)
    yield data
