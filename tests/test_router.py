from __future__ import annotations

from typing import Any

import pytest

from jarvis.providers.base import (
    Capability,
    ChatResult,
    ErrorKind,
    Message,
    Provider,
    ProviderError,
    ProviderState,
    ProviderStatus,
    Transcription,
)
from jarvis.providers.network import NetworkStatus
from jarvis.providers.router import AllProvidersFailedError, ProviderRouter


class FakeProvider(Provider):
    def __init__(
        self,
        name: str,
        errors: list[ErrorKind] | None = None,
        caps: set[Capability] | None = None,
        status: ProviderStatus | None = None,
    ) -> None:
        self.name = name
        self.display_name = name.title()
        self.errors = list(errors or [])
        self.calls = 0
        self._caps = caps or {Capability.CHAT, Capability.STT}
        self._status = status or ProviderStatus(ProviderState.READY, latency_ms=10)

    def capabilities(self) -> set[Capability]:
        return self._caps

    def model_for(self, capability: Capability) -> str:
        return f"{self.name}-model"

    def missing_config(self) -> str | None:
        return None

    async def probe(self) -> ProviderStatus:
        return self._status

    async def chat(
        self, messages: list[Message], *, tools: Any = None, vision: bool = False
    ) -> ChatResult:
        self.calls += 1
        if self.errors:
            raise ProviderError(self.display_name, self.errors.pop(0), "failed")
        return ChatResult(self.display_name, f"{self.name}-model", f"hi from {self.name}")

    async def transcribe(self, wav: bytes) -> Transcription:
        self.calls += 1
        if self.errors:
            raise ProviderError(self.display_name, self.errors.pop(0), "failed")
        return Transcription("hello", self.display_name, "stt")


async def online() -> NetworkStatus:
    return NetworkStatus(True, 5)


def make(*providers: FakeProvider, retries: int = 2) -> ProviderRouter:
    return ProviderRouter(
        {p.name: p for p in providers},
        [p.name for p in providers],
        {p.name: retries for p in providers},
        backoff_s=0,
        network_check=online,
    )


async def test_uses_first_provider_and_reports_it() -> None:
    groq, gemini = FakeProvider("groq"), FakeProvider("gemini")
    router = make(groq, gemini)
    result = await router.chat([])
    assert result.provider == "Groq"
    assert router.last_used == ("Groq", "groq-model")
    assert gemini.calls == 0


async def test_retries_transient_errors_then_succeeds() -> None:
    groq = FakeProvider("groq", [ErrorKind.QUOTA, ErrorKind.UNAVAILABLE])
    result = await make(groq).chat([])
    assert groq.calls == 3
    assert result.provider == "Groq"


async def test_retries_are_bounded_then_falls_back() -> None:
    groq = FakeProvider("groq", [ErrorKind.UNAVAILABLE] * 10)
    gemini = FakeProvider("gemini")
    router = make(groq, gemini, retries=2)
    result = await router.chat([])
    assert groq.calls == 3  # 1 try + 2 retries
    assert result.provider == "Gemini"  # never pretends it was Groq
    assert router.statuses["groq"].state is ProviderState.ERROR


async def test_a_timeout_moves_on_at_once() -> None:
    groq = FakeProvider("groq", [ErrorKind.TIMEOUT] * 10)
    gemini = FakeProvider("gemini")
    router = make(groq, gemini, retries=2)
    result = await router.chat([])
    assert groq.calls == 1 and result.provider == "Gemini"


async def test_auth_error_is_not_retried_and_marks_provider() -> None:
    groq = FakeProvider("groq", [ErrorKind.AUTH])
    ollama = FakeProvider("ollama")
    router = make(groq, ollama)
    result = await router.chat([])
    assert groq.calls == 1
    assert result.provider == "Ollama"
    assert router.statuses["groq"].state is ProviderState.AUTH_ERROR


async def test_unusable_providers_are_skipped() -> None:
    groq = FakeProvider("groq", status=ProviderStatus(ProviderState.NO_KEY, "set GROQ_API_KEY"))
    gemini = FakeProvider("gemini")
    router = make(groq, gemini)
    await router.probe_all()
    result = await router.chat([])
    assert groq.calls == 0
    assert result.provider == "Gemini"


async def test_capability_routing_for_vision() -> None:
    groq = FakeProvider("groq")  # no vision
    gemini = FakeProvider("gemini", caps={Capability.CHAT, Capability.VISION})
    result = await make(groq, gemini).chat([], vision=True)
    assert result.provider == "Gemini"
    assert groq.calls == 0


async def test_all_failed_explains_each_provider() -> None:
    groq = FakeProvider("groq", [ErrorKind.NETWORK] * 5)
    ollama = FakeProvider("ollama", status=ProviderStatus(ProviderState.OFFLINE, "not running"))
    router = make(groq, ollama, retries=1)
    await router.probe_all()
    with pytest.raises(AllProvidersFailedError) as info:
        await router.chat([])
    assert [a.provider for a in info.value.attempts] == ["Groq", "Ollama"]
    assert "not running" in str(info.value)
    assert router.statuses["groq"].state is ProviderState.OFFLINE
    assert not router.any_usable()


async def test_transcribe_skips_providers_without_stt() -> None:
    ollama = FakeProvider("ollama", caps={Capability.CHAT})
    with pytest.raises(AllProvidersFailedError) as info:
        await make(ollama).transcribe(b"")
    assert info.value.attempts == []


async def test_probe_all_updates_network_and_notifies() -> None:
    changes: list[int] = []
    router = ProviderRouter(
        {"groq": FakeProvider("groq")},
        ["groq"],
        {},
        on_change=lambda: changes.append(1),
        network_check=online,
    )
    await router.probe_all()
    assert router.network.online
    assert router.statuses["groq"].state is ProviderState.READY
    assert changes
