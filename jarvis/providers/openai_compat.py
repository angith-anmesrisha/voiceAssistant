"""HTTP implementation of the OpenAI chat-completions protocol, shared by all providers."""

from __future__ import annotations

import base64
import json
import os
import time
import uuid
from typing import Any

import httpx

from jarvis.app.config import ProviderSettings
from jarvis.providers.base import (
    Capability,
    ChatResult,
    ErrorKind,
    Message,
    Provider,
    ProviderError,
    ProviderState,
    ProviderStatus,
    ToolCall,
    Transcription,
)

_PROBE_TIMEOUT_S = 5.0


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200] or response.reason_phrase
    if isinstance(body, list) and body:
        body = body[0]
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict):
            return str(error.get("message") or error)[:300]
        return str(error)[:300]
    return str(body)[:300]


def _kind_for_status(status: int, message: str = "") -> ErrorKind:
    # Gemini answers a bad key with 400 "API key not valid", not 401.
    if status in (401, 403) or "api key" in message.lower():
        return ErrorKind.AUTH
    if status == 429:
        return ErrorKind.QUOTA
    if status >= 500:
        return ErrorKind.UNAVAILABLE
    return ErrorKind.BAD_REQUEST


def clean_assistant_message(message: dict[str, Any]) -> Message:
    """Keep only fields every provider accepts when the message is sent back."""
    cleaned: Message = {"role": "assistant", "content": message.get("content") or ""}
    if message.get("tool_calls"):
        cleaned["tool_calls"] = [
            {
                "id": call.get("id"),
                "type": "function",
                "function": {
                    "name": call["function"]["name"],
                    "arguments": call["function"].get("arguments") or "{}",
                },
            }
            for call in message["tool_calls"]
        ]
    return cleaned


def parse_tool_calls(message: dict[str, Any]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for raw in message.get("tool_calls") or []:
        function = raw.get("function") or {}
        arguments = function.get("arguments") or "{}"
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                parsed = {"_invalid_json": arguments}
        else:
            parsed = arguments
        calls.append(
            ToolCall(
                id=str(raw.get("id") or f"call_{uuid.uuid4().hex[:8]}"),
                name=str(function.get("name", "")),
                arguments=parsed if isinstance(parsed, dict) else {"value": parsed},
            )
        )
    return calls


class OpenAICompatibleProvider(Provider):
    def __init__(
        self,
        name: str,
        display_name: str,
        settings: ProviderSettings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self.display_name = display_name
        self.settings = settings
        self._client = client or httpx.AsyncClient()

    # --- configuration ---

    def _api_key(self) -> str | None:
        env = self.settings.api_key_env
        if not env:
            return None
        value = os.environ.get(env, "").strip()
        return value or None

    def missing_config(self) -> str | None:
        if not self.settings.enabled:
            return "disabled in settings"
        if self.settings.api_key_env and not self._api_key():
            return f"set the {self.settings.api_key_env} environment variable"
        return None

    def capabilities(self) -> set[Capability]:
        caps = {Capability.CHAT}
        if self.settings.vision_model:
            caps.add(Capability.VISION)
        if self.settings.stt_model:
            caps.add(Capability.STT)
        return caps

    def model_for(self, capability: Capability) -> str:
        if capability is Capability.VISION:
            return self.settings.vision_model
        if capability is Capability.STT:
            return self.settings.stt_model
        return self.settings.chat_model

    def _headers(self) -> dict[str, str]:
        key = self._api_key()
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _url(self, path: str) -> str:
        return self.settings.base_url.rstrip("/") + "/" + path.lstrip("/")

    # --- HTTP ---

    async def _post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        if (reason := self.missing_config()) is not None:
            raise ProviderError(self.display_name, ErrorKind.NO_KEY, reason)
        try:
            response = await self._client.post(
                self._url(path),
                headers=self._headers(),
                timeout=self.settings.timeout_s,
                **kwargs,
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(self.display_name, ErrorKind.TIMEOUT, "timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                self.display_name, ErrorKind.NETWORK, f"can't connect ({type(exc).__name__})"
            ) from exc
        if response.status_code >= 400:
            message = _error_message(response)
            raise ProviderError(
                self.display_name,
                _kind_for_status(response.status_code, message),
                f"HTTP {response.status_code}: {message}",
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(
                self.display_name, ErrorKind.UNAVAILABLE, "returned an invalid response"
            ) from exc
        if not isinstance(data, dict):
            raise ProviderError(self.display_name, ErrorKind.UNAVAILABLE, "unexpected response")
        return data

    async def probe(self) -> ProviderStatus:
        if not self.settings.enabled:
            return ProviderStatus(ProviderState.DISABLED, "disabled in settings")
        if (reason := self.missing_config()) is not None:
            return ProviderStatus(ProviderState.NO_KEY, reason)
        started = time.perf_counter()
        try:
            response = await self._client.get(
                self._url("models"), headers=self._headers(), timeout=_PROBE_TIMEOUT_S
            )
        except httpx.HTTPError as exc:
            return ProviderStatus(ProviderState.OFFLINE, f"unreachable ({type(exc).__name__})")
        latency = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            message = _error_message(response)
            if _kind_for_status(response.status_code, message) is ErrorKind.AUTH:
                return ProviderStatus(ProviderState.AUTH_ERROR, message)
            return ProviderStatus(
                ProviderState.ERROR, f"HTTP {response.status_code}: {message}", latency_ms=latency
            )
        return self._check_models(response, latency)

    def _check_models(self, response: httpx.Response, latency: int) -> ProviderStatus:
        return ProviderStatus(ProviderState.READY, latency_ms=latency)

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        vision: bool = False,
    ) -> ChatResult:
        capability = Capability.VISION if vision else Capability.CHAT
        model = self.model_for(capability)
        if not model:
            raise ProviderError(
                self.display_name, ErrorKind.UNSUPPORTED, f"no {capability.value} model set"
            )
        body: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            body["tools"] = tools
        data = await self._post("chat/completions", json=body)
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                self.display_name, ErrorKind.UNAVAILABLE, "reply had no message"
            ) from exc
        return ChatResult(
            provider=self.display_name,
            model=str(data.get("model") or model),
            content=(message.get("content") or "").strip(),
            tool_calls=parse_tool_calls(message),
            message=clean_assistant_message(message),
        )

    async def transcribe(self, wav: bytes) -> Transcription:
        model = self.settings.stt_model
        if not model:
            raise ProviderError(self.display_name, ErrorKind.UNSUPPORTED, "no speech-to-text")
        data = await self._post(
            "audio/transcriptions",
            files={"file": ("speech.wav", wav, "audio/wav")},
            data={"model": model, "response_format": "json", "temperature": "0"},
        )
        return Transcription(str(data.get("text", "")).strip(), self.display_name, model)


def audio_part(wav: bytes) -> dict[str, Any]:
    return {
        "type": "input_audio",
        "input_audio": {"data": base64.b64encode(wav).decode("ascii"), "format": "wav"},
    }


def image_part(jpeg: bytes) -> dict[str, Any]:
    encoded = base64.b64encode(jpeg).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}}
