"""Application settings.

Non-secret settings are stored as JSON in the data directory. The Gemini API key is
never stored here; see :mod:`hitodama.app.secrets`. Model names live here so that no
logic module hard-codes them.
"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field, ValidationError

from hitodama.app.paths import settings_file


class ModelSettings(BaseModel):
    # Placeholders until M1 verifies current names against ai.google.dev.
    live: str = "gemini-live-2.5-flash-preview"
    text: str = "gemini-2.5-flash"
    vision: str = "gemini-2.5-flash"


class HotkeySettings(BaseModel):
    push_to_talk: str = "ctrl+alt+space"
    dictation: str = "ctrl+win"
    circle_to_ask: str = "ctrl+shift"


class AudioSettings(BaseModel):
    input_sample_rate: int = 16_000
    output_sample_rate: int = 24_000
    input_device: str | None = None
    output_device: str | None = None


class LoadoutSettings(BaseModel):
    soul: str = "ember"
    personality: str = "calm"
    custom_personality: str = ""
    voice: str | None = None
    disabled_abilities: list[str] = Field(default_factory=list)


class Settings(BaseModel):
    log_level: str = "INFO"
    start_with_windows: bool = False
    show_captions: bool = True
    models: ModelSettings = Field(default_factory=ModelSettings)
    hotkeys: HotkeySettings = Field(default_factory=HotkeySettings)
    audio: AudioSettings = Field(default_factory=AudioSettings)
    loadout: LoadoutSettings = Field(default_factory=LoadoutSettings)


def load_settings(path: Path | None = None) -> Settings:
    """Load settings, falling back to defaults if the file is missing or invalid."""
    path = path or settings_file()
    if not path.exists():
        return Settings()
    try:
        return Settings.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError) as exc:
        logger.warning("Settings file unreadable ({}); using defaults", type(exc).__name__)
        return Settings()


def save_settings(settings: Settings, path: Path | None = None) -> None:
    """Write settings atomically (write temp file, then replace)."""
    path = path or settings_file()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(settings.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)
