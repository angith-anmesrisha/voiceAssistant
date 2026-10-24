"""Application settings.

Non-secret settings live in ``settings.json`` in the data directory. API keys are
never stored here: they are read from environment variables (see ``ProviderSettings``).
Model names live here so that no logic module hard-codes them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, Field, ValidationError, model_validator

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
    # Longest reply asked for. Keeps replies short for speech, and keeps each request
    # inside per-minute token limits (they count the requested output too).
    max_output_tokens: int = 2048
    # Other models of this provider, tried in order when the one in use is missing for
    # this account, rate-limited, busy or too slow.
    fallback_models: list[str] = Field(default_factory=list)
    # Prompt-injection classifier run over outside text (clipboard, screen, files) before
    # it reaches the chat model. Empty = no screening on this provider.
    guard_model: str = ""
    # Attack probability (0..1) at or above which text is withheld.
    guard_threshold: float = 0.8
    timeout_s: float = 20.0
    # Extra tries on the same provider, only for "server busy" (5xx). Other errors move on.
    max_retries: int = 1


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
        # When qwen is rate-limited (free tier: 8K tokens/min, 1K requests/day per model),
        # these Groq production models answer instead (console.groq.com/docs/models).
        fallback_models=["openai/gpt-oss-120b", "llama-3.3-70b-versatile"],
        # The owner asked for Llama Prompt Guard 2 (86M). It is a classifier, not a chat
        # model (console.groq.com/docs/model/meta-llama/llama-prompt-guard-2-86m, 512
        # token input), so it screens untrusted text; qwen still answers.
        guard_model="meta-llama/llama-prompt-guard-2-86m",
    )


def _gemini() -> ProviderSettings:
    # OpenAI-compatible endpoint, checked against ai.google.dev/gemini-api/docs/openai.
    return ProviderSettings(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key_env="GEMINI_API_KEY",
        # Gemini is the backup, so it should answer fast: 3.5 Flash-Lite is Google's fastest
        # current model (ai.google.dev/gemini-api/docs/models, 2026-10-24), then 3.8 Flash
        # (timed out on the owner's key), 3.6 Flash, and 2.5 Flash (legacy access only).
        # A model that is slow, busy, rate-limited or missing for the key is skipped.
        chat_model="gemini-3.5-flash-lite",
        vision_model="gemini-3.5-flash-lite",
        stt_model="gemini-3.5-flash-lite",
        fallback_models=["gemini-3.8-flash", "gemini-3.6-flash", "gemini-2.5-flash"],
        # "none" disables thinking on 2.5 Flash; 3.x models get "low" (they can't turn it off).
        reasoning_effort="none",
    )


def _ollama() -> ProviderSettings:
    return ProviderSettings(
        base_url="http://localhost:11434/v1",
        # Qwen 3.5 handles chat, tools and images. Reasoning is off for speed on an iGPU.
        # qwen3.5:4b (~3.5 GB) is several times faster than qwen3.5:latest (the 9B, ~7 GB),
        # which took 33 s for a one-word answer on the owner's Iris Xe laptop. It still
        # sees images and calls tools. The 9B is used if only that one is installed.
        chat_model="qwen3.5:4b",
        # Preferred model for looking at the screen. If it isn't installed or can't see
        # images, any installed model with vision is used instead (see OllamaProvider).
        vision_model="qwen3.5:4b",
        fallback_models=["qwen3.5:latest"],
        reasoning_effort="none",
        # Local models on an iGPU are slow, the first request also loads the model, and the
        # tool list makes for a long prompt. A timeout isn't retried.
        timeout_s=240.0,
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

    @model_validator(mode="before")
    @classmethod
    def _fill_in_defaults(cls, data: Any) -> Any:
        """Let settings.json change just one field, e.g. {"ollama": {"chat_model": ...}}."""
        if isinstance(data, dict):
            data = dict(data)
            for name, default in (("groq", _groq), ("gemini", _gemini), ("ollama", _ollama)):
                part = data.get(name)
                if isinstance(part, dict):
                    data[name] = {**default().model_dump(), **part}
        return data

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
    # "kokoro": JARVIS's own British voice, run on this PC (free, offline, unlimited;
    # the model, ~350 MB, is downloaded once). "sapi": Windows' built-in voice only.
    tts_engine: Literal["kokoro", "sapi"] = "kokoro"
    # Kokoro voice: bm_george (default), bm_fable, bm_lewis or bm_daniel (British male).
    neural_voice: str = "bm_george"
    neural_speed: float = Field(default=1.05, ge=0.7, le=1.3)  # under 1 is more measured
    download_voice: bool = True
    # Windows SAPI voice name substring (e.g. "David", "Hazel"); empty = system default.
    # Used until the neural voice is ready, or always with tts_engine "sapi".
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
    # Ask Medium-risk confirmations out loud and accept a spoken yes or no (the
    # buttons still work). High risk always needs CONFIRM typed in the window.
    voice_confirm: bool = True
    # Say "On it." when a spoken request is still being worked on after a moment.
    acknowledge: bool = True


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
    # Look-act-check steps per request: operating an app's window takes several.
    max_tool_rounds: int = 15
    max_typed_chars: int = 2000
    # Weather when no place is named (a "home city" you tell JARVIS is used first).
    home_place: str = ""
    # Language and country of news headlines, e.g. "en-IN" or "en-GB".
    news_locale: str = "en-US"


class RoutineSettings(BaseModel):
    """Things JARVIS brings up by itself. Each can be turned off."""

    # Before noon, on the first moment you're at the PC: date, weather, reminders,
    # yesterday's recap and a few headlines.
    briefing: bool = True
    briefing_headlines: int = 3
    # A short spoken nudge after this many hours at the PC without a break (0 = off).
    check_in_hours: float = 2.0
    # Low battery, and processor or memory flat out for a couple of minutes.
    hardware_alerts: bool = True
    battery_low: int = Field(default=20, ge=5, le=50)
    # One- or two-sentence summaries of each session, mentioned in the next briefing.
    summaries: bool = True
    # No check-ins or topic news between these hours (local time, 24 h clock).
    quiet_from: int = Field(default=22, ge=0, le=23)
    quiet_until: int = Field(default=8, ge=0, le=23)


class Settings(BaseModel):
    log_level: str = "INFO"
    history_messages: int = 20
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    hotkeys: HotkeySettings = Field(default_factory=HotkeySettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)
    routines: RoutineSettings = Field(default_factory=RoutineSettings)


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
