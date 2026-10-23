"""Test doubles for the Live API and audio devices (no network, no sound card)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any

from google.genai import types


def audio_msg(data: bytes) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        server_content=types.LiveServerContent(
            model_turn=types.Content(
                role="model", parts=[types.Part(inline_data=types.Blob(data=data))]
            )
        )
    )


def turn_complete_msg() -> types.LiveServerMessage:
    return types.LiveServerMessage(server_content=types.LiveServerContent(turn_complete=True))


class FakeSession:
    """Mimics google.genai AsyncSession: receive() yields one turn, then returns."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.inbox: asyncio.Queue[types.LiveServerMessage | BaseException] = asyncio.Queue()

    async def send_realtime_input(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)

    async def receive(self) -> AsyncIterator[types.LiveServerMessage]:
        while True:
            item = await self.inbox.get()
            if isinstance(item, BaseException):
                raise item
            yield item
            content = item.server_content
            if content is not None and content.turn_complete:
                return


class FakeConnector:
    """Hands out FakeSessions; can be told to fail the next connects."""

    def __init__(self) -> None:
        self.sessions: list[FakeSession] = []
        self.configs: list[types.LiveConnectConfig] = []
        self.models: list[str] = []
        self.fail_with: list[BaseException] = []
        self.connected = asyncio.Event()

    def __call__(
        self, model: str, config: types.LiveConnectConfig
    ) -> contextlib.AbstractAsyncContextManager[FakeSession]:
        @contextlib.asynccontextmanager
        async def cm() -> AsyncIterator[FakeSession]:
            self.models.append(model)
            self.configs.append(config)
            if self.fail_with:
                raise self.fail_with.pop(0)
            session = FakeSession()
            self.sessions.append(session)
            self.connected.set()
            yield session

        return cm()


class FakeMic:
    def __init__(self) -> None:
        self.on_chunk: Callable[[bytes], None] | None = None
        self.fail = False

    def start(self, on_chunk: Callable[[bytes], None]) -> None:
        from hitodama.core.audio_io import AudioDeviceError

        if self.fail:
            raise AudioDeviceError("Microphone unavailable: test")
        self.on_chunk = on_chunk

    def stop(self) -> None:
        self.on_chunk = None


class FakeSpeaker:
    def __init__(self, on_drained: Callable[[], None]) -> None:
        self.on_drained = on_drained
        self.played: list[bytes] = []
        self.pending = 0
        self.opened = False
        self.cleared = 0

    @property
    def has_pending_audio(self) -> bool:
        return self.pending > 0

    def open(self) -> None:
        self.opened = True

    def write(self, pcm: bytes) -> None:
        self.played.append(pcm)
        self.pending += len(pcm)

    def clear(self) -> None:
        self.pending = 0
        self.cleared += 1

    def close(self) -> None:
        self.opened = False
        self.pending = 0

    def drain(self) -> None:
        """Simulate the device finishing playback."""
        self.pending = 0
        self.on_drained()


async def settle(rounds: int = 20) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)
