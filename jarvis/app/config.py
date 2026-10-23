"""Application settings.

Non-secret settings live in ``settings.json`` in the data directory. API keys are
never stored here: they are read from environment variables (see ``ProviderSettings``).
Model names live here so that no logic module hard-codes them.
"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field, ValidationError

from jarvis.app.paths import settings_file


class ProviderSettings(BaseModel):
    """One OpenAI-compatible chat provider."""

    enabled: bool = True
    base_url: str
    # Name of the environment variable holding the API key; empty means no key needed.
    api_key_env: str = ""
    chat_model: str
    # Empty means this provider is not used for screen analysis.
    vision_model: str = ""
    # Empty means this provider is not used for speech-to-text.
    stt_model: str = ""
    timeout_s: float = 30.0
    max_retries: int = 2


def _groq() -> ProviderSettings:
    # Model IDs checked against console.groq.com/docs/models on 2026-10-23.
    return ProviderSettings(
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        chat_model="openai/gpt-oss-120b",
        stt_model="whisper-large-v3-turbo",
    )


def _gemini() -> ProviderSettings:
    # OpenAI-compatible endpoint, checked against ai.google.dev/gemini-api/docs/openai.
    return ProviderSettings(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key_env="GEMINI_API_KEY",
        chat_model="gemini-3.8-flash",
        vision_model="gemini-3.8-flash",
        stt_model="gemini-3.8-flash",
    )


def _ollama() -> ProviderSettings:
    return ProviderSettings(
        base_url="http://localhost:11434/v1",
        chat_model="llama3.2",
        timeout_s=120.0,  # local models on an iGPU can be slow
        max_retries=1,
    )


class ProvidersConfig(BaseModel):
    # Tried in this order; unavailable providers are skipped.
    priority: list[str] = Field(default_factory=lambda: ["groq", "gemini", "ollama"])
    groq: ProviderSettings = Field(default_factory=_groq)
    gemini: ProviderSettings = Field(default_factory=_gemini)
    ollama: ProviderSettings = Field(default_factory=_ollama)
    # How often to re-check network and provider availability.
    probe_interval_s: float = 20.0

    def get(self, name: str) -> ProviderSettings:
        settings = getattr(self, name, None)
        if not isinstance(settings, ProviderSettings):
            raise KeyError(name)
        return settings


class HotkeySettings(BaseModel):
    push_to_talk: str = "ctrl+alt+space"


class VoiceSettings(BaseModel):
    input_sample_rate: int = 16_000
    input_device: str | None = None
    # Speak replies to spoken requests. Typed requests are answered in text only.
    speak_replies: bool = True
    # Windows SAPI voice name substring (e.g. "David", "Zira"); empty = system default.
    tts_voice: str = ""
    tts_rate: int = 1  # SAPI rate, -10..10
    max_recording_s: float = 60.0


def _default_apps() -> dict[str, str]:
    # name -> program or URI. Only these can be opened by the assistant.
    return {
        "notepad": "notepad.exe",
        "calculator": "calc.exe",
        "paint": "mspaint.exe",
        "file explorer": "explorer.exe",
        "task manager": "taskmgr.exe",
        "settings": "ms-settings:",
        "snipping tool": "ms-screenclip:",
        "spotify": "spotify:",
        "vs code": "code",
    }


def _default_sites() -> list[str]:
    return [
        "google.com",
        "youtube.com",
        "wikipedia.org",
        "github.com",
        "amazon.com",
        "amazon.in",
        "maps.google.com",
        "gmail.com",
        "mail.google.com",
        "chatgpt.com",
        "stackoverflow.com",
    ]


class ToolSettings(BaseModel):
    approved_apps: dict[str, str] = Field(default_factory=_default_apps)
    # Opening these domains (and subdomains) needs no confirmation; others ask first.
    trusted_sites: list[str] = Field(default_factory=_default_sites)
    max_tool_rounds: int = 5
    max_typed_chars: int = 2000


class Settings(BaseModel):
    log_level: str = "INFO"
    history_messages: int = 20
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    hotkeys: HotkeySettings = Field(default_factory=HotkeySettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)


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
