"""The three concrete providers."""

from __future__ import annotations

from typing import Any

import httpx

from jarvis.app.config import ProvidersConfig
from jarvis.providers.base import (
    Capability,
    ErrorKind,
    Provider,
    ProviderError,
    ProviderState,
    ProviderStatus,
    Transcription,
)
from jarvis.providers.openai_compat import OpenAICompatibleProvider, audio_part

_TRANSCRIBE_PROMPT = (
    "Transcribe this audio exactly as spoken, in the language spoken. "
    "Reply with the transcript only. If there is no speech, reply with nothing."
)


class GroqProvider(OpenAICompatibleProvider):
    """Primary online provider."""


class GeminiProvider(OpenAICompatibleProvider):
    """Secondary online provider (Gemini's OpenAI-compatible endpoint)."""

    async def transcribe(self, wav: bytes) -> Transcription:
        # The compatibility layer has no /audio/transcriptions; send the audio to chat.
        model = self.settings.stt_model
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
