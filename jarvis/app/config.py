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
    # Sent as the OpenAI "reasoning_effort" field when set (e.g. "none" turns off a
    # local model's thinking, which is slow on an integrated GPU).
    reasoning_effort: str = ""
    timeout_s: float = 30.0
    max_retries: int = 2


def _groq() -> ProviderSettings:
    # Checked against console.groq.com/docs/model/qwen/qwen3.8-27b on 2026-10-23: tools
    # and image input; reasoning_effort "none" turns thinking off (replies come faster).
    return ProviderSettings(
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        chat_model="qwen/qwen3.8-27b",
        vision_model="qwen/qwen3.8-27b",
        stt_model="whisper-large-v3-turbo",
        reasoning_effort="none",
    )


def _gemini() -> ProviderSettings:
    # OpenAI-compatible endpoint, checked against ai.google.dev/gemini-api/docs/openai.
    return ProviderSettings(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key_env="GEMINI_API_KEY",
        # The owner asked for 2.5 models. ai.google.dev/gemini-api/docs/deprecations
        # (2026-10-23): still served, but only to accounts that used 2.5 before; if yours
        # reports the model missing, set these back to "gemini-3.8-flash".
        chat_model="gemini-2.5-flash",
        vision_model="gemini-2.5-flash",
        stt_model="gemini-2.5-flash",
        reasoning_effort="none",  # "none" disables thinking on 2.5 Flash (OpenAI compat docs)
    )


def _ollama() -> ProviderSettings:
    return ProviderSettings(
        base_url="http://localhost:11434/v1",
        # Qwen 3.5 handles chat, tools and images. Reasoning is off for speed on an iGPU.
        chat_model="qwen3.5",
        # Preferred model for looking at the screen. If it isn't installed or can't see
        # images, any installed model with vision is used instead (see OllamaProvider).
        vision_model="qwen3.5",
        reasoning_effort="none",
        timeout_s=120.0,  # local models on an iGPU can be slow
        max_retries=1,
    )


class ProvidersConfig(BaseModel):
    # Tried in this order; unavailable providers are skipped.
    priority: list[str] = Field(default_factory=lambda: ["groq", "gemini", "ollama"])
    # Provider the user picked (top bar, or "use Gemini from now on"); "" = automatic.
    preferred: str = ""
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
    push_to_talk: str = "alt+`"


class VoiceSettings(BaseModel):
    input_sample_rate: int = 16_000
    input_device: str | None = None
    # Speak replies to spoken requests. Typed requests are answered in text only.
    speak_replies: bool = True
    # Windows SAPI voice name substring (e.g. "David", "Zira"); empty = system default.
    tts_voice: str = ""
    tts_rate: int = 1  # SAPI rate, -10..10
    max_recording_s: float = 60.0
    # Offline wake word (Windows speech recognition). Say it, pause, then speak.
    wake_word: bool = True
    wake_phrases: list[str] = Field(default_factory=lambda: ["Jarvis", "Hey Jarvis"])
    wake_min_confidence: str = "normal"  # low | normal | high
    # After answering a wake-word request, keep listening this long for a follow-up
    # without the wake word. 0 turns it off.
    follow_up_s: float = 30.0


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


def save_choice(section: str, key: str, value: object, path: Path | None = None) -> None:
    """Remember one choice made in the app, leaving the rest of settings.json as it is."""
    path = path or settings_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    part = data.setdefault(section, {})
    if not isinstance(part, dict):
        part = data[section] = {}
    part[key] = value
    try:
        Settings.model_validate(data)
    except ValidationError:
        logger.warning("Not saving {}.{}: settings file is invalid", section, key)
        return
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def save_settings(settings: Settings, path: Path | None = None) -> None:
    """Write settings atomically (write temp file, then replace)."""
    path = path or settings_file()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(settings.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)
