"""Common provider interface.

Every provider speaks the OpenAI chat-completions message format (Groq, Gemini and
Ollama all offer it), so conversation history and tool calls move between providers
unchanged when one fails over to the next.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

Message = dict[str, Any]


class Capability(StrEnum):
    CHAT = "chat"
    VISION = "vision"
    STT = "speech-to-text"


class ProviderState(StrEnum):
    CHECKING = "checking"
    READY = "ready"
    NO_KEY = "no key"
    OFFLINE = "offline"
    AUTH_ERROR = "key rejected"
    MODEL_MISSING = "model missing"
    ERROR = "error"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    state: ProviderState
    detail: str = ""
    latency_ms: int | None = None

    @property
    def usable(self) -> bool:
        # CHECKING and ERROR are worth a try; the others will certainly fail.
        return self.state in (ProviderState.READY, ProviderState.CHECKING, ProviderState.ERROR)


class ErrorKind(StrEnum):
    NO_KEY = "no key"
    AUTH = "auth"
    QUOTA = "quota"
    BAD_REQUEST = "bad request"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    NETWORK = "network"
    UNSUPPORTED = "unsupported"


# Worth another try on the same provider. A timeout or a dropped connection moves on to
# the next provider straight away: waiting for the same one again is what made fallback
# feel stuck.
_RETRYABLE = {ErrorKind.UNAVAILABLE}


class ProviderError(Exception):
    def __init__(self, provider: str, kind: ErrorKind, message: str, status: int = 0) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.kind = kind
        self.message = message
        self.status = status  # HTTP status, 0 if there was no response

    @property
    def retryable(self) -> bool:
        return self.kind in _RETRYABLE


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ChatResult:
    """A model reply, tagged with the provider and model that actually produced it."""

    provider: str
    model: str
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    # The assistant message to append to history (role/content/tool_calls only).
    message: Message = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Transcription:
    text: str
    provider: str
    model: str


class Provider(ABC):
    """A chat model backend."""

    name: str
    display_name: str

    @abstractmethod
    def capabilities(self) -> set[Capability]: ...

    @abstractmethod
    def model_for(self, capability: Capability) -> str: ...

    @abstractmethod
    def missing_config(self) -> str | None:
        """Why this provider can't be used at all (e.g. no API key), or None."""

    @abstractmethod
    async def probe(self) -> ProviderStatus: ...

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        vision: bool = False,
    ) -> ChatResult: ...

    @abstractmethod
    async def transcribe(self, wav: bytes) -> Transcription: ...

    # --- self-test ("test the AI providers") ---

    def diagnostic_models(self) -> list[str]:
        """Chat models to test, in order (the one in use first)."""
        return [self.model_for(Capability.CHAT)]

    async def ping(self, model: str, prompt: str, timeout_s: float) -> ChatResult:
        """One tiny request to one model, no tools and no fallback."""
        return await self.chat([{"role": "user", "content": prompt}])

    def adopt(self, model: str) -> None:  # noqa: B027 (optional hook)
        """Use ``model`` for chat from now on (it just passed the self-test)."""
