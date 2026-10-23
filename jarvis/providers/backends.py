"""The three concrete providers."""

from __future__ import annotations

import httpx

from jarvis.app.config import ProvidersConfig
from jarvis.providers.base import (
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
    """Offline provider running on this PC."""

    def _check_models(self, response: httpx.Response, latency: int) -> ProviderStatus:
        try:
            names = {str(item.get("id", "")) for item in response.json().get("data", [])}
        except (ValueError, AttributeError):
            names = set()
        wanted = [self.settings.chat_model, self.settings.vision_model]
        missing = [m for m in wanted if m and not _has_model(names, m)]
        if missing:
            return ProviderStatus(
                ProviderState.MODEL_MISSING,
                "run: " + " && ".join(f"ollama pull {m}" for m in missing),
                latency_ms=latency,
            )
        return ProviderStatus(ProviderState.READY, latency_ms=latency)

    async def probe(self) -> ProviderStatus:
        status = await super().probe()
        if status.state is ProviderState.OFFLINE:
            return ProviderStatus(ProviderState.OFFLINE, "Ollama isn't running")
        return status


def _has_model(names: set[str], model: str) -> bool:
    return (
        model in names or f"{model}:latest" in names or any(n.split(":")[0] == model for n in names)
    )


def build_providers(config: ProvidersConfig, client: httpx.AsyncClient) -> dict[str, Provider]:
    return {
        "groq": GroqProvider("groq", "Groq", config.groq, client),
        "gemini": GeminiProvider("gemini", "Gemini", config.gemini, client),
        "ollama": OllamaProvider("ollama", "Ollama", config.ollama, client),
    }
