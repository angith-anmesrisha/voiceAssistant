from __future__ import annotations

import asyncio

import pytest

from hitodama.app.config import Settings
from hitodama.app.events import (
    ApiKeyChanged,
    EventBus,
    PushToTalkPressed,
    PushToTalkReleased,
    Transcript,
    UserNotice,
)
from hitodama.app.state import AppState, StateChanged, StateMachine
from hitodama.core import live_session as ls
from hitodama.core.live_session import LiveErrorKind, LiveHandler, LiveSession
from hitodama.core.voice_loop import VoiceLoop
from tests.fakes import FakeConnector, FakeMic, FakeSpeaker, audio_msg, settle, turn_complete_msg


class Rig:
    def __init__(self, *, has_key: bool = True) -> None:
        self.bus = EventBus()
        self.state = StateMachine(self.bus)
        self.settings = Settings()
        self.connector = FakeConnector()
        self.mic = FakeMic()
        self.speaker: FakeSpeaker | None = None
        self.notices: list[UserNotice] = []
        self.transcripts: list[Transcript] = []
        self.states: list[AppState] = []
        self.has_key = has_key

        async def on_notice(event: UserNotice) -> None:
            self.notices.append(event)

        async def on_transcript(event: Transcript) -> None:
            self.transcripts.append(event)

        async def on_state(event: StateChanged) -> None:
            self.states.append(event.current)

        self.bus.subscribe(UserNotice, on_notice)
        self.bus.subscribe(Transcript, on_transcript)
        self.bus.subscribe(StateChanged, on_state)

        def session_factory(handler: LiveHandler) -> LiveSession | None:
            if not self.has_key:
                return None
            return LiveSession(self.connector, ["m"], handler, lambda: "SYS")

        def speaker_factory(on_drained: object) -> FakeSpeaker:
            self.speaker = FakeSpeaker(on_drained)  # type: ignore[arg-type]
            return self.speaker

        self.loop = VoiceLoop(
            self.bus, self.state, self.settings, session_factory, self.mic, speaker_factory
        )

    def now(self) -> AppState:
        # A method call (not attribute access) so mypy doesn't narrow across awaits.
        return self.state.state

    @property
    def spk(self) -> FakeSpeaker:
        assert self.speaker is not None
        return self.speaker

    async def press(self) -> None:
        await self.bus.publish(PushToTalkPressed())
        await asyncio.wait_for(self.connector.connected.wait(), 1)
        await settle()

    async def release(self) -> None:
        await self.bus.publish(PushToTalkReleased())
        await settle()

    def server(self) -> asyncio.Queue[object]:
        return self.connector.sessions[-1].inbox  # type: ignore[return-value]


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ls, "_BACKOFF_S", (0.0,))


async def test_full_turn() -> None:
    rig = Rig()
    await rig.press()
    assert rig.now() is AppState.LISTENING
    assert rig.mic.on_chunk is not None
    rig.mic.on_chunk(b"\x00\x01")
    await rig.release()
    assert rig.now() is AppState.THINKING
    assert rig.mic.on_chunk is None
    sent = [next(iter(item)) for item in rig.connector.sessions[0].sent]
    assert sent == ["activity_start", "audio", "activity_end"]

    rig.server().put_nowait(audio_msg(b"hello"))
    await settle()
    assert rig.now() is AppState.SPEAKING
    rig.server().put_nowait(turn_complete_msg())
    await settle()
    assert rig.now() is AppState.SPEAKING  # still playing
    rig.spk.drain()
    await settle()
    assert rig.now() is AppState.IDLE
    assert rig.spk.played == [b"hello"]
    assert rig.states == [AppState.LISTENING, AppState.THINKING, AppState.SPEAKING, AppState.IDLE]
    await rig.loop.shutdown()


async def test_barge_in_stops_playback_and_drops_old_reply() -> None:
    rig = Rig()
    await rig.press()
    await rig.release()
    rig.server().put_nowait(audio_msg(b"long answer part 1"))
    await settle()
    assert rig.now() is AppState.SPEAKING

    await rig.press()  # talk over it
    assert rig.now() is AppState.LISTENING
    assert rig.spk.cleared >= 1
    rig.server().put_nowait(audio_msg(b"long answer part 2"))  # old reply still streaming
    rig.server().put_nowait(turn_complete_msg())
    await settle()
    assert rig.spk.played == [b"long answer part 1"]
    assert rig.now() is AppState.LISTENING

    await rig.release()
    rig.server().put_nowait(audio_msg(b"new answer"))
    await settle()
    assert rig.spk.played[-1] == b"new answer"
    assert rig.now() is AppState.SPEAKING
    await rig.loop.shutdown()


async def test_server_interruption_clears_playback() -> None:
    from google.genai import types

    rig = Rig()
    await rig.press()
    await rig.release()
    rig.server().put_nowait(audio_msg(b"x"))
    rig.server().put_nowait(
        types.LiveServerMessage(server_content=types.LiveServerContent(interrupted=True))
    )
    await settle()
    assert rig.spk.cleared == 1
    await rig.loop.shutdown()


async def test_no_api_key_shows_notice() -> None:
    rig = Rig(has_key=False)
    await rig.bus.publish(PushToTalkPressed())
    await settle()
    assert rig.now() is AppState.IDLE
    assert rig.notices and "API key" in rig.notices[0].title
    await rig.bus.publish(PushToTalkReleased())  # harmless
    await settle()
    assert rig.now() is AppState.IDLE


async def test_mic_failure_is_reported_not_raised() -> None:
    rig = Rig()
    rig.mic.fail = True
    await rig.bus.publish(PushToTalkPressed())
    await settle()
    assert rig.now() is AppState.IDLE
    assert any("Audio" in n.title for n in rig.notices)


async def test_fatal_error_resets_to_idle_with_friendly_message() -> None:
    rig = Rig()
    rig.loop.on_fatal_error(LiveErrorKind.QUOTA, "429")
    await settle()
    assert rig.now() is AppState.IDLE
    assert "usage limit" in rig.notices[-1].message


async def test_connection_lost_mid_turn() -> None:
    from google.genai import errors

    rig = Rig()
    await rig.press()
    await rig.release()
    rig.server().put_nowait(errors.APIError(1006, {"message": "Abnormal closure."}))
    await settle(60)
    assert rig.now() is AppState.IDLE
    assert any("Connection lost" in n.title for n in rig.notices)
    await rig.loop.shutdown()


async def test_reply_timeout() -> None:
    rig = Rig()
    rig.settings.live.reply_timeout_s = 0.01
    await rig.press()
    await rig.release()
    await asyncio.sleep(0.05)
    await settle()
    assert rig.now() is AppState.IDLE
    assert any("No reply" in n.title for n in rig.notices)
    await rig.loop.shutdown()


async def test_transcripts_published_when_captions_on() -> None:
    rig = Rig()
    rig.loop.on_transcript("user", "what time is it")
    await settle()
    assert rig.transcripts == [Transcript(role="user", text="what time is it")]
    rig.settings.show_captions = False
    rig.loop.on_transcript("user", "again")
    await settle()
    assert len(rig.transcripts) == 1


async def test_api_key_change_drops_session() -> None:
    rig = Rig()
    await rig.press()
    await rig.release()
    await rig.bus.publish(ApiKeyChanged())
    await settle()
    assert rig.loop._session is None
    await rig.loop.shutdown()


async def test_bad_config_notice_includes_server_reason() -> None:
    rig = Rig()
    rig.loop.on_fatal_error(LiveErrorKind.BAD_CONFIG, "models/x is not found")
    await settle()
    assert "models/x is not found" in rig.notices[-1].message
