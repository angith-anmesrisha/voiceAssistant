from __future__ import annotations

from pathlib import Path

import pytest

from hitodama.app import secrets
from hitodama.app.config import Settings, load_settings, save_settings
from hitodama.app.paths import settings_file


def test_defaults_when_missing() -> None:
    settings = load_settings()
    assert settings == Settings()
    assert settings.hotkeys.push_to_talk == "ctrl+alt+space"


def test_round_trip(isolated_env: Path) -> None:
    settings = Settings()
    settings.loadout.personality = "witty"
    save_settings(settings)
    assert settings_file().parent == isolated_env
    assert load_settings().loadout.personality == "witty"


def test_corrupt_file_falls_back_to_defaults() -> None:
    settings_file().write_text("{not json", encoding="utf-8")
    assert load_settings() == Settings()


def test_settings_file_never_contains_api_key() -> None:
    secrets.set_api_key("AIza-test-key")
    save_settings(load_settings())
    assert "AIza-test-key" not in settings_file().read_text(encoding="utf-8")


def test_api_key_round_trip() -> None:
    assert secrets.get_api_key() is None
    assert not secrets.has_api_key()
    secrets.set_api_key("  AIza-abc  ")
    assert secrets.get_api_key() == "AIza-abc"
    secrets.delete_api_key()
    secrets.delete_api_key()  # idempotent
    assert not secrets.has_api_key()


def test_empty_api_key_rejected() -> None:
    with pytest.raises(ValueError):
        secrets.set_api_key("   ")
