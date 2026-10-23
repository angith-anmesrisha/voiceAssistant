"""The three concrete providers."""

from __future__ import annotations

from typing import Any

import httpx

from jarvis.app.config import ProvidersConfig
from jarvis.providers.base import (
    Capability,
    ErrorKind,
    Message,
    Provider,
    ProviderError,
    ProviderState,
    ProviderStatus,
    Transcription,
)
from jarvis.providers.openai_compat import OpenAICompatibleProvider, audio_part

# Gemini 3 wants the thought signature it attached to each tool call sent back with the
# history. Tool calls made by another provider (after a fallback) have none; Google
# documents this placeholder for history it didn't produce.
_NO_SIGNATURE = {"google": {"thought_signature": "skip_thought_signature_validator"}}

_GEMINI_HINTS = {
    "FAILED_PRECONDITION": (
        "Google says a prerequisite isn't met for this key's project (usually billing, or "
        "the free tier not being available for the account or region). Fix it in Google "
        "AI Studio > Billing; JARVIS can't change it"
    ),
    "PERMISSION_DENIED": "this key isn't allowed to use the Gemini API (check the key's project)",
    "RESOURCE_EXHAUSTED": "the key's Gemini quota is used up for now",
}

_TRANSCRIBE_PROMPT = (
    "Transcribe this audio exactly as spoken, in the language spoken. "
    "Reply with the transcript only. If there is no speech, reply with nothing."
)


class GroqProvider(OpenAICompatibleProvider):
    """Primary online provider."""


class GeminiProvider(OpenAICompatibleProvider):
    """Secondary online provider (Gemini's OpenAI-compatible endpoint).

    If the configured model isn't available to this account (2.5 models are only served to
    accounts that used them before), the next of ``fallback_models`` that is gets used.
    """

    def reasoning_for(self, model: str) -> str:
        effort = self.settings.reasoning_effort
        # "none" (no thinking) only exists for 2.5 Flash models; 3.x can't turn it off.
        if effort == "none" and not (model.startswith("gemini-2.5") and "pro" not in model):
            return "low"
        return effort

    def outgoing(self, messages: list[Message]) -> list[Message]:
        out: list[Message] = []
        for message in messages:
            calls = message.get("tool_calls") if isinstance(message, dict) else None
            if calls and any("extra_content" not in c for c in calls):
                message = {
                    **message,
                    "tool_calls": [
                        c if "extra_content" in c else {**c, "extra_content": _NO_SIGNATURE}
                        for c in calls
                    ],
                }
            out.append(message)
        return out

    def explain(self, error: ProviderError) -> ProviderError:
        for code, hint in _GEMINI_HINTS.items():
            if code in error.message:
                return ProviderError(
                    error.provider, error.kind, f"{error.message} ({hint})", error.status
                )
        return error

    def _check_models(self, response: httpx.Response, latency: int) -> ProviderStatus:
        """Switch to a fallback model up front if the configured one isn't listed."""
        try:
            listed = {
                str(item.get("id", "")).removeprefix("models/")
                for item in response.json().get("data", [])
            }
        except (ValueError, AttributeError):
            listed = set()
        if listed:
            for capability in (Capability.CHAT, Capability.VISION, Capability.STT):
                configured = self.configured_model(capability)
                if not configured or configured in listed:
                    continue
                usable = [m for m in self._candidates(configured) if m in listed]
                if usable:
                    self._use_model(configured, usable[0])
                else:
                    return ProviderStatus(
                        ProviderState.MODEL_MISSING,
                        f"{configured} isn't available to this key",
                        latency_ms=latency,
                    )
        return ProviderStatus(ProviderState.READY, latency_ms=latency)

    async def transcribe(self, wav: bytes) -> Transcription:
        # The compatibility layer has no /audio/transcriptions; send the audio to chat.
        model = self.model_for(Capability.STT)
        if not model:
            raise ProviderError(self.display_name, ErrorKind.UNSUPPORTED, "no speech-to-text")
        data = await self._post(
            "chat/completions",
            json={
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": _TRANSCRIBE_PROMPT}, audio_part(wav)],
                    }
                ],
            },
        )
        try:
            text = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                self.display_name, ErrorKind.UNAVAILABLE, "reply had no message"
            ) from exc
        return Transcription(str(text).strip(), self.display_name, model)


class OllamaProvider(OpenAICompatibleProvider):
    """Offline provider running on this PC.

    The chat model must be installed. For screen questions, the configured vision model is used
    if it can see images; otherwise any installed model that can (per Ollama's
    ``/api/show`` capabilities) is picked, so no extra download is needed.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._installed: list[str] = []
        self._vision_model = ""
        self._capability_cache: dict[str, bool] = {}

    def capabilities(self) -> set[Capability]:
        caps = {Capability.CHAT}
        if self._vision_model:
            caps.add(Capability.VISION)
        return caps

    def model_for(self, capability: Capability) -> str:
        if capability is Capability.VISION:
            return self._vision_model
        if capability is Capability.STT:
            return ""
        return self.settings.chat_model

    def _check_models(self, response: httpx.Response, latency: int) -> ProviderStatus:
        try:
            names = [str(item.get("id", "")) for item in response.json().get("data", [])]
        except (ValueError, AttributeError):
            names = []
        self._installed = [n for n in names if n]
        chat = self.settings.chat_model
        if not _has_model(set(self._installed), chat):
            return ProviderStatus(
                ProviderState.MODEL_MISSING, f"run: ollama pull {chat}", latency_ms=latency
            )
        return ProviderStatus(ProviderState.READY, latency_ms=latency)

    async def probe(self) -> ProviderStatus:
        status = await super().probe()
        if status.state is ProviderState.OFFLINE:
            self._vision_model = ""
            return ProviderStatus(ProviderState.OFFLINE, "Ollama isn't running")
        if status.state is ProviderState.READY:
            self._vision_model = await self._find_vision_model()
        return status

    async def _find_vision_model(self) -> str:
        preferred = self.settings.vision_model
        candidates = [n for n in self._installed if preferred and _same_model(n, preferred)]
        candidates += sorted((n for n in self._installed if n not in candidates), key=_size_hint)
        for name in candidates[:8]:
            if await self._can_see(name):
                return name
        return ""

    async def _can_see(self, model: str) -> bool:
        if model in self._capability_cache:
            return self._capability_cache[model]
        root = self.settings.base_url.rstrip("/").removesuffix("/v1")
        try:
            response = await self._client.post(
                f"{root}/api/show", json={"model": model}, timeout=5.0
            )
            caps = response.json().get("capabilities", []) if response.status_code < 400 else []
        except (httpx.HTTPError, ValueError, AttributeError):
            return False  # not cached: try again on the next probe
        result = isinstance(caps, list) and "vision" in caps
        self._capability_cache[model] = result
        return result


def _same_model(name: str, wanted: str) -> bool:
    return name == wanted or name == f"{wanted}:latest" or name.removesuffix(":latest") == wanted


def _size_hint(name: str) -> float:
    """Smaller models first (kinder to an iGPU). Tags like ':4b' or ':0.8b' give the size."""
    tag = name.partition(":")[2].lower()
    if tag.endswith("b"):
        try:
            return float(tag[:-1])
        except ValueError:
            pass
    return 1000.0


def _has_model(names: set[str], model: str) -> bool:
    # Exact tags only: "qwen3.5" means qwen3.5:latest, not qwen3.5:4b.
    return model in names or f"{model}:latest" in names


def build_providers(config: ProvidersConfig, client: httpx.AsyncClient) -> dict[str, Provider]:
    return {
        "groq": GroqProvider("groq", "Groq", config.groq, client),
        "gemini": GeminiProvider("gemini", "Gemini", config.gemini, client),
        "ollama": OllamaProvider("ollama", "Ollama", config.ollama, client),
    }
