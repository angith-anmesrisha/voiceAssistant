"""HTTP implementation of the OpenAI chat-completions protocol, shared by all providers."""

from __future__ import annotations

import base64
import json
import time
import uuid
from typing import Any

import httpx
from loguru import logger

from jarvis.app.config import ProviderSettings
from jarvis.app.keys import get_key
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
            message = str(error.get("message") or error)
            status = error.get("status")  # Google's code, e.g. FAILED_PRECONDITION
            if isinstance(status, str) and status and status not in message:
                message = f"{status}: {message}"
            return message[:300]
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
    """Keep only the fields needed when the message is sent back.

    ``extra_content`` (Gemini's thought signature on a tool call) is kept here and
    removed again by :meth:`OpenAICompatibleProvider.outgoing` for providers that don't
    know it.
    """
    cleaned: Message = {"role": "assistant", "content": message.get("content") or ""}
    if message.get("tool_calls"):
        calls = []
        for call in message["tool_calls"]:
            kept: dict[str, Any] = {
                "id": call.get("id"),
                "type": "function",
                "function": {
                    "name": call["function"]["name"],
                    "arguments": call["function"].get("arguments") or "{}",
                },
            }
            if isinstance(call.get("extra_content"), dict):
                kept["extra_content"] = call["extra_content"]
            calls.append(kept)
        cleaned["tool_calls"] = calls
    return cleaned


def strip_extra_content(messages: list[Message]) -> list[Message]:
    out: list[Message] = []
    for message in messages:
        calls = message.get("tool_calls") if isinstance(message, dict) else None
        if calls and any("extra_content" in c for c in calls):
            message = {
                **message,
                "tool_calls": [{k: v for k, v in c.items() if k != "extra_content"} for c in calls],
            }
        out.append(message)
    return out


_MODEL_GONE = (
    "not found",
    "not supported",
    "does not exist",
    "no longer available",
    "not available",
    "decommissioned",
    "is not enabled",
    "model_not_found",
)


# 400s about the account (Gemini), not the model: the other models get them too.
_ACCOUNT_WIDE = ("FAILED_PRECONDITION", "PERMISSION_DENIED", "billing")

_COOL_DOWN_S = 60.0
_SLOW_COOL_DOWN_S = 300.0


def _combined(provider: str, errors: list[tuple[str, ProviderError]]) -> ProviderError:
    """One error for a provider whose models all failed, naming each model's reason."""
    if len(errors) == 1:
        return errors[0][1]
    last = errors[-1][1]
    detail = "; ".join(f"{model}: {exc.message}" for model, exc in errors)
    return ProviderError(provider, last.kind, detail, last.status)


def model_unavailable(error: ProviderError) -> bool:
    """The provider works, but not this model for this account: try another model."""
    if error.kind is not ErrorKind.BAD_REQUEST:
        return False
    text = error.message.lower()
    return error.status == 404 or ("model" in text and any(w in text for w in _MODEL_GONE))


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
        # Names and words speech-to-text should expect ("Priya, Kochi, Valorant").
        self.vocabulary = ""
        self._client = client or httpx.AsyncClient()
        # (capability, configured model) -> the model that works for this account now
        self._swapped: dict[tuple[Capability, str], str] = {}
        self._dead: set[str] = set()  # not available to this account at all
        self._cooling: dict[str, float] = {}  # rate-limited model -> when to try it again

    # --- configuration ---

    def _api_key(self) -> str | None:
        env = self.settings.api_key_env
        if not env:
            return None
        return get_key(env)

    def missing_config(self) -> str | None:
        if not self.settings.enabled:
            return "disabled in settings"
        if self.settings.api_key_env and not self._api_key():
            return "no API key: add one with the key button under AI PROVIDERS"
        return None

    def capabilities(self) -> set[Capability]:
        caps = {Capability.CHAT}
        if self.settings.vision_model:
            caps.add(Capability.VISION)
        if self.settings.stt_model:
            caps.add(Capability.STT)
        return caps

    def configured_model(self, capability: Capability) -> str:
        if capability is Capability.VISION:
            return self.settings.vision_model
        if capability is Capability.STT:
            return self.settings.stt_model
        return self.settings.chat_model

    def model_for(self, capability: Capability) -> str:
        model = self.configured_model(capability)
        return self._swapped.get((capability, model), model)

    def fallbacks_for(self, capability: Capability) -> list[str]:
        """Other models to use when the configured one can't answer (chat only by default:
        not every fallback can see images)."""
        return self.settings.fallback_models if capability is Capability.CHAT else []

    def _candidates(self, capability: Capability) -> list[str]:
        """Models to try, in order: the one in use, the configured one, the fallbacks.
        Unavailable models are left out; rate-limited ones go last."""
        names = [
            self.model_for(capability),
            self.configured_model(capability),
            *self.fallbacks_for(capability),
        ]
        now = time.monotonic()
        ready: list[str] = []
        cooling: list[str] = []
        for name in names:
            if not name or name in ready or name in cooling or name in self._dead:
                continue
            (cooling if self._cooling.get(name, 0.0) > now else ready).append(name)
        return ready + cooling

    def _use_model(self, capability: Capability, model: str) -> None:
        configured = self.configured_model(capability)
        if model != self.model_for(capability):
            logger.warning("{}: now using {} for {}", self.display_name, model, capability.value)
        if model == configured:
            self._swapped.pop((capability, configured), None)
        else:
            self._swapped[(capability, configured)] = model

    def reasoning_for(self, model: str) -> str:
        return self.settings.reasoning_effort

    def diagnostic_models(self) -> list[str]:
        names = [
            self.model_for(Capability.CHAT),
            self.configured_model(Capability.CHAT),
            *self.fallbacks_for(Capability.CHAT),
        ]
        return list(dict.fromkeys(n for n in names if n))

    async def ping(self, model: str, prompt: str, timeout_s: float) -> ChatResult:
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 64,
        }
        if effort := self.reasoning_for(model):
            body["reasoning_effort"] = effort
        data = await self._post("chat/completions", json=body, timeout_s=timeout_s)
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
            tool_calls=[],
            message={"role": "assistant", "content": message.get("content") or ""},
        )

    def adopt(self, model: str) -> None:
        self._cooling.pop(model, None)
        self._dead.discard(model)
        self._use_model(Capability.CHAT, model)

    def outgoing(self, messages: list[Message]) -> list[Message]:
        """Messages as this provider accepts them."""
        return strip_extra_content(messages)

    def explain(self, error: ProviderError) -> ProviderError:
        """Hook for providers to make an error message clearer."""
        return error

    def _headers(self) -> dict[str, str]:
        key = self._api_key()
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _url(self, path: str) -> str:
        return self.settings.base_url.rstrip("/") + "/" + path.lstrip("/")

    # --- HTTP ---

    async def _post(
        self, path: str, *, timeout_s: float | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        if (reason := self.missing_config()) is not None:
            raise ProviderError(self.display_name, ErrorKind.NO_KEY, reason)
        try:
            response = await self._client.post(
                self._url(path),
                headers=self._headers(),
                timeout=timeout_s or self.settings.timeout_s,
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
            raise self.explain(
                ProviderError(
                    self.display_name,
                    _kind_for_status(response.status_code, message),
                    f"HTTP {response.status_code}: {message}",
                    status=response.status_code,
                )
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
        configured = self.configured_model(capability)
        if not configured:
            raise ProviderError(
                self.display_name, ErrorKind.UNSUPPORTED, f"no {capability.value} model set"
            )
        outgoing = self.outgoing(messages)
        candidates = self._candidates(capability)
        if not candidates:
            raise ProviderError(
                self.display_name, ErrorKind.BAD_REQUEST, "none of the configured models works"
            )
        errors: list[tuple[str, ProviderError]] = []
        data: dict[str, Any] | None = None
        for model in candidates:
            body: dict[str, Any] = {"model": model, "messages": outgoing}
            if effort := self.reasoning_for(model):
                body["reasoning_effort"] = effort
            if self.settings.max_output_tokens:
                # Also keeps requests inside per-minute token limits, which count the
                # requested output (Groq's free tier allows 8K tokens a minute).
                body["max_tokens"] = self.settings.max_output_tokens
            if tools:
                body["tools"] = tools
            try:
                data = await self._post("chat/completions", json=body)
            except ProviderError as exc:
                errors.append((model, exc))
                logger.warning("{} {} failed: {}", self.display_name, model, exc.message[:200])
                if model_unavailable(exc):
                    self._dead.add(model)  # this account can't use it: never again
                elif exc.kind is ErrorKind.QUOTA:
                    self._cooling[model] = time.monotonic() + _COOL_DOWN_S
                elif exc.kind is ErrorKind.TIMEOUT:
                    # Too slow now: start the next requests on another model, and let the
                    # router try the next provider for this one.
                    self._cooling[model] = time.monotonic() + _SLOW_COOL_DOWN_S
                    raise _combined(self.display_name, errors) from exc
                elif exc.kind not in (ErrorKind.UNAVAILABLE, ErrorKind.BAD_REQUEST) or any(
                    code in exc.message for code in _ACCOUNT_WIDE
                ):
                    # Key, billing or account: another model won't help.
                    raise _combined(self.display_name, errors) from exc
                # Busy, or this model rejected the request (Groq answers 400
                # "tool_use_failed" when a model garbles a tool call): try the next one.
                continue  # try the next model on the same provider
            self._use_model(capability, model)
            break
        if data is None:
            raise _combined(self.display_name, errors)
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                self.display_name, ErrorKind.UNAVAILABLE, "reply had no message"
            ) from exc
        content = (message.get("content") or "").strip()
        if data["choices"][0].get("finish_reason") == "length" and content:
            logger.warning("{} reply was cut off at the output limit", self.display_name)
            content += "…"
        return ChatResult(
            provider=self.display_name,
            model=str(data.get("model") or model),
            content=content,
            tool_calls=parse_tool_calls(message),
            message=clean_assistant_message(message),
        )

    def guard_ready(self) -> bool:
        return bool(self.settings.guard_model) and self.missing_config() is None

    async def classify(self, text: str) -> str:
        """Run the prompt-injection classifier; returns its raw reply (score or label)."""
        body = {
            "model": self.settings.guard_model,
            "messages": [{"role": "user", "content": text}],
        }
        data = await self._post("chat/completions", json=body)
        try:
            return str(data["choices"][0]["message"].get("content") or "")
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ProviderError(
                self.display_name, ErrorKind.UNAVAILABLE, "guard reply had no message"
            ) from exc

    async def transcribe(self, wav: bytes) -> Transcription:
        model = self.settings.stt_model
        if not model:
            raise ProviderError(self.display_name, ErrorKind.UNSUPPORTED, "no speech-to-text")
        data = await self._post(
            "audio/transcriptions",
            files={"file": ("speech.wav", wav, "audio/wav")},
            data=self._transcription_fields(model),
        )
        return Transcription(heard_text(data), self.display_name, model)

    def _transcription_fields(self, model: str) -> dict[str, str]:
        fields = {"model": model, "response_format": "verbose_json", "temperature": "0"}
        if self.vocabulary:
            # Whisper's prompt steers spelling towards these words (OpenAI-compatible field).
            fields["prompt"] = self.vocabulary
        return fields


# Whisper invents short phrases ("I'm going to go.", "Thank you.") from noise or silence.
# Segments it rates as probably not speech, or is very unsure about, are dropped.
_NO_SPEECH_PROB = 0.6
_MIN_AVG_LOGPROB = -1.0


def heard_text(data: dict[str, Any]) -> str:
    segments = data.get("segments")
    if not isinstance(segments, list) or not segments:
        return str(data.get("text", "")).strip()
    kept = [
        str(seg.get("text", "")).strip()
        for seg in segments
        if isinstance(seg, dict)
        and float(seg.get("no_speech_prob", 0.0)) < _NO_SPEECH_PROB
        and float(seg.get("avg_logprob", 0.0)) > _MIN_AVG_LOGPROB
    ]
    return " ".join(t for t in kept if t).strip()


def audio_part(wav: bytes) -> dict[str, Any]:
    return {
        "type": "input_audio",
        "input_audio": {"data": base64.b64encode(wav).decode("ascii"), "format": "wav"},
    }


def image_part(jpeg: bytes) -> dict[str, Any]:
    encoded = base64.b64encode(jpeg).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}}
