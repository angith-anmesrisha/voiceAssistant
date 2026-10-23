"""Chooses a provider by priority, with bounded retries and graceful fallback.

Results always carry the provider and model that really answered; nothing here
pretends a model was used if it wasn't.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

from loguru import logger

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
from jarvis.providers.network import NetworkStatus, check_internet

T = TypeVar("T")

_ERROR_STATE = {
    ErrorKind.NO_KEY: ProviderState.NO_KEY,
    ErrorKind.AUTH: ProviderState.AUTH_ERROR,
    ErrorKind.NETWORK: ProviderState.OFFLINE,
}


_PING = "Reply with just the word OK."


@dataclass(frozen=True, slots=True)
class Diagnosis:
    provider: str
    model: str
    ok: bool
    ms: int
    detail: str  # the reply, or the provider's exact error
    blame: str  # "" | "account" | "service" | "network" | "setup" | "jarvis"


def _blame(error: ProviderError) -> str:
    """Whose side a failure is on, so the user knows whether to fix a key or tell us."""
    if error.kind in (ErrorKind.AUTH, ErrorKind.QUOTA) or "FAILED_PRECONDITION" in error.message:
        return "account"
    if error.kind in (ErrorKind.NO_KEY, ErrorKind.UNSUPPORTED):
        return "setup"
    if error.kind is ErrorKind.UNAVAILABLE:
        return "service"
    if error.kind in (ErrorKind.NETWORK, ErrorKind.TIMEOUT):
        return "network"
    return "jarvis"  # a request the provider rejected: likely ours to fix


@dataclass(frozen=True, slots=True)
class Attempt:
    provider: str
    error: str


class AllProvidersFailedError(Exception):
    def __init__(self, capability: Capability, attempts: list[Attempt]) -> None:
        self.capability = capability
        self.attempts = attempts
        if attempts:
            detail = "; ".join(f"{a.provider}: {a.error}" for a in attempts)
        else:
            detail = "no provider is set up for this"
        super().__init__(f"No AI provider could handle {capability.value}. {detail}")


class ProviderRouter:
    def __init__(
        self,
        providers: Mapping[str, Provider],
        priority: list[str],
        retries: Mapping[str, int],
        *,
        on_change: Callable[[], None] | None = None,
        backoff_s: float = 0.5,
        network_check: Callable[[], Awaitable[NetworkStatus]] = check_internet,
    ) -> None:
        self._providers = dict(providers)
        self._priority = [name for name in priority if name in self._providers]
        self._retries = dict(retries)
        self._on_change = on_change
        self._backoff_s = backoff_s
        self._network_check = network_check
        self.statuses: dict[str, ProviderStatus] = {
            name: ProviderStatus(ProviderState.CHECKING) for name in self._priority
        }
        self.network = NetworkStatus(online=False)
        self.last_used: tuple[str, str] | None = None  # (provider, model)
        # Chosen by the user ("use Gemini from now on"): tried first, others still back it up.
        self.preferred: str | None = None
        # What the router is doing right now ("Asking Gemini…"), for the UI, and the
        # providers that failed before the last answer (so a fallback is visible).
        self.activity = ""
        self.skipped: list[Attempt] = []

    @property
    def ordered(self) -> list[Provider]:
        names = self._priority
        if self.preferred in names:
            names = [self.preferred, *(n for n in names if n != self.preferred)]
        return [self._providers[name] for name in names]

    @property
    def names(self) -> list[str]:
        return list(self._priority)

    def provider(self, name: str) -> Provider | None:
        return self._providers.get(name) if name in self._priority else None

    def set_preferred(self, name: str | None) -> None:
        """Put one provider first (None = the configured priority)."""
        if name is not None and name not in self._priority:
            raise KeyError(name)
        if name != self.preferred:
            self.preferred = name
            if self._on_change is not None:
                self._on_change()

    def provider_named(self, display_name: str) -> Provider | None:
        return next((p for p in self.ordered if p.display_name == display_name), None)

    def first_usable(self) -> Provider | None:
        """The provider a chat request would try first."""
        return next((p for p in self.ordered if self.statuses[p.name].usable), None)

    def any_usable(self) -> bool:
        return any(self.statuses[p.name].usable for p in self.ordered)

    def _notify(self) -> None:
        if self._on_change is not None:
            self._on_change()

    def _set_activity(self, text: str) -> None:
        if text != self.activity:
            self.activity = text
            self._notify()

    def _set_status(self, name: str, status: ProviderStatus) -> None:
        if self.statuses.get(name) != status:
            self.statuses[name] = status
            if self._on_change is not None:
                self._on_change()

    async def probe_all(self) -> None:
        network, statuses = await asyncio.gather(
            self._network_check(), asyncio.gather(*(p.probe() for p in self.ordered))
        )
        changed = network != self.network
        self.network = network
        for provider, status in zip(self.ordered, statuses, strict=True):
            self._set_status(provider.name, status)
        if changed and self._on_change is not None:
            self._on_change()

    async def run_probes(self, interval_s: float) -> None:
        while True:
            try:
                await self.probe_all()
            except Exception as exc:  # never let the status loop die
                logger.warning("Provider probe failed: {}", type(exc).__name__)
            await asyncio.sleep(interval_s)

    async def _route(
        self, capability: Capability, call: Callable[[Provider], Awaitable[T]]
    ) -> tuple[T, Provider]:
        attempts: list[Attempt] = []
        failed: list[Attempt] = []  # tried and failed (not just skipped as unusable)
        for provider in self.ordered:
            if capability not in provider.capabilities():
                continue
            status = self.statuses[provider.name]
            if not status.usable:
                attempts.append(Attempt(provider.display_name, status.detail or status.state))
                continue
            max_attempts = 1 + max(0, self._retries.get(provider.name, 0))
            last_error: ProviderError | None = None
            for attempt in range(max_attempts):
                if failed and attempt == 0:
                    self._set_activity(
                        f"{failed[-1].provider} didn't answer, asking {provider.display_name}…"
                    )
                elif attempt:
                    self._set_activity(
                        f"Asking {provider.display_name} again ({attempt + 1}/{max_attempts})…"
                    )
                else:
                    self._set_activity(f"Asking {provider.display_name}…")
                try:
                    result = await call(provider)
                except ProviderError as exc:
                    logger.warning(
                        "{} {} failed ({}), attempt {}/{}",
                        provider.display_name,
                        capability.value,
                        exc.kind.value,
                        attempt + 1,
                        max_attempts,
                    )
                    last_error = exc
                    if not exc.retryable or attempt + 1 == max_attempts:
                        break
                    await asyncio.sleep(self._backoff_s * (2**attempt))
                    continue
                self._set_status(
                    provider.name, ProviderStatus(ProviderState.READY, latency_ms=status.latency_ms)
                )
                self.skipped = failed
                self._set_activity("")
                return result, provider
            assert last_error is not None
            attempts.append(Attempt(provider.display_name, last_error.message))
            failed.append(Attempt(provider.display_name, last_error.message))
            state = _ERROR_STATE.get(last_error.kind)
            if state is not None:
                self._set_status(provider.name, ProviderStatus(state, last_error.message))
            elif last_error.kind in (ErrorKind.UNAVAILABLE, ErrorKind.TIMEOUT):
                self._set_status(
                    provider.name, ProviderStatus(ProviderState.ERROR, last_error.message)
                )
        self.skipped = failed
        self._set_activity("")
        raise AllProvidersFailedError(capability, attempts)

    async def diagnose(self) -> list[Diagnosis]:
        """Send a tiny request to every provider (no retries, no fallback) and report."""

        async def check(provider: Provider) -> Diagnosis:
            status = await provider.probe()
            self._set_status(provider.name, status)
            if status.state in (ProviderState.NO_KEY, ProviderState.DISABLED):
                return Diagnosis(provider.display_name, "", False, 0, status.detail, "setup")
            if status.state is ProviderState.OFFLINE and provider.name == "ollama":
                return Diagnosis(provider.display_name, "", False, 0, status.detail, "setup")
            started = time.perf_counter()
            try:
                result = await provider.chat([{"role": "user", "content": _PING}])
            except ProviderError as exc:
                ms = int((time.perf_counter() - started) * 1000)
                model = provider.model_for(Capability.CHAT)
                return Diagnosis(provider.display_name, model, False, ms, exc.message, _blame(exc))
            except Exception as exc:  # a bug on our side: say so
                return Diagnosis(provider.display_name, "", False, 0, type(exc).__name__, "jarvis")
            ms = int((time.perf_counter() - started) * 1000)
            self._set_status(provider.name, ProviderStatus(ProviderState.READY, latency_ms=ms))
            return Diagnosis(provider.display_name, result.model, True, ms, result.content, "")

        providers = [self._providers[name] for name in self._priority]
        return list(await asyncio.gather(*(check(p) for p in providers)))

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        vision: bool = False,
    ) -> ChatResult:
        capability = Capability.VISION if vision else Capability.CHAT
        result, _ = await self._route(
            capability, lambda p: p.chat(messages, tools=tools, vision=vision)
        )
        self.last_used = (result.provider, result.model)
        if self._on_change is not None:
            self._on_change()
        return result

    async def transcribe(self, wav: bytes) -> Transcription:
        result, _ = await self._route(Capability.STT, lambda p: p.transcribe(wav))
        return result
