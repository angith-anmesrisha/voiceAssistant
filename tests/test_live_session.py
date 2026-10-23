from __future__ import annotations

import asyncio
from typing import Any

import pytest
from google.genai import errors, types

from hitodama.core import live_session as ls
from hitodama.core.live_session import LiveErrorKind, LiveSession, classify_error, error_detail
from tests.fakes import FakeConnector, audio_msg, settle, turn_complete_msg


class RecordingHandler:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    def on_audio(self, pcm: bytes) -> None:
        self.events.append(("audio", pcm))

    def on_interrupted(self) -> None:
        self.events.append(("interrupted", None))

    def on_turn_complete(self) -> None:
        self.events.append(("turn_complete", None))

    def on_transcript(self, role: str, text: str) -> None:
        self.events.append(("transcript", (role, text)))

    def on_connection_lost(self) -> None:
        self.events.append(("lost", None))

    def on_fatal_error(self, kind: LiveErrorKind, detail: str) -> None:
        self.events.append(("fatal", kind))


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ls, "_BACKOFF_S", (0.0,))


def make(connector: FakeConnector, handler: RecordingHandler) -> LiveSession:
    return LiveSession(connector, ["test-live-model"], handler, lambda: "SYSTEM", voice="Kore")


def api_error(code: int, message: str) -> errors.APIError:
    """What the SDK raises when the server closes the socket with a reason."""
    try:
        errors.APIError.raise_error(code, message, None)
    except errors.APIError as exc:
        return exc
    raise AssertionError("unreachable")


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (
            errors.APIError(1008, {"message": "API key not valid. Please pass a valid API key."}),
            LiveErrorKind.INVALID_KEY,
        ),
        (
            errors.APIError(1011, {"message": "You exceeded your current quota"}),
            LiveErrorKind.QUOTA,
        ),
        (errors.ClientError(429, {"message": "Too many requests"}), LiveErrorKind.QUOTA),
        (
            errors.APIError(1008, {"message": "User location is not supported for the API use."}),
            LiveErrorKind.REGION,
        ),
        (errors.APIError(1008, {"message": "models/foo is not found"}), LiveErrorKind.BAD_CONFIG),
        (
            errors.APIError(1011, {"message": "Deadline expired before operation could complete."}),
            LiveErrorKind.TRANSIENT,
        ),
        (errors.APIError(1006, {"message": "Abnormal closure."}), LiveErrorKind.TRANSIENT),
        (api_error(1007, "Precondition check failed."), LiveErrorKind.ACCOUNT),
        (OSError("Network is unreachable"), LiveErrorKind.TRANSIENT),
        (TimeoutError(), LiveErrorKind.TRANSIENT),
    ],
)
def test_classify_error(exc: BaseException, kind: LiveErrorKind) -> None:
    assert classify_error(exc) is kind


async def test_config_uses_push_to_talk_and_resumption() -> None:
    connector, handler = FakeConnector(), RecordingHandler()
    session = make(connector, handler)
    session.start()
    await asyncio.wait_for(connector.connected.wait(), 1)
    config = connector.configs[0]
    assert connector.models == ["test-live-model"]
    assert config.response_modalities == [types.Modality.AUDIO]
    assert config.realtime_input_config is not None
    vad = config.realtime_input_config.automatic_activity_detection
    assert vad is not None and vad.disabled is True
    assert config.session_resumption is not None
    assert config.context_window_compression is not None
    assert config.system_instruction == "SYSTEM"
    assert config.speech_config is not None
    await session.stop()


async def test_audio_queued_before_connect_is_sent_in_order() -> None:
    connector, handler = FakeConnector(), RecordingHandler()
    session = make(connector, handler)
    session.begin_turn()
    session.send_audio(b"\x01\x02")
    session.end_turn()
    session.start()
    await asyncio.wait_for(connector.connected.wait(), 1)
    await settle()
    sent = connector.sessions[0].sent
    assert [next(iter(item)) for item in sent] == ["activity_start", "audio", "activity_end"]
    assert sent[1]["audio"].mime_type == "audio/pcm;rate=16000"
    await session.stop()


async def test_dispatches_audio_transcripts_and_turn_complete() -> None:
    connector, handler = FakeConnector(), RecordingHandler()
    session = make(connector, handler)
    session.start()
    await asyncio.wait_for(connector.connected.wait(), 1)
    inbox = connector.sessions[0].inbox
    inbox.put_nowait(
        types.LiveServerMessage(
            server_content=types.LiveServerContent(
                input_transcription=types.Transcription(text="hi there")
            )
        )
    )
    inbox.put_nowait(audio_msg(b"abc"))
    inbox.put_nowait(turn_complete_msg())
    inbox.put_nowait(audio_msg(b"second turn"))  # receive() is re-entered per turn
    await settle()
    assert handler.events == [
        ("transcript", ("user", "hi there")),
        ("audio", b"abc"),
        ("turn_complete", None),
        ("audio", b"second turn"),
    ]
    await session.stop()


async def test_reconnects_with_resumption_handle_after_drop() -> None:
    connector, handler = FakeConnector(), RecordingHandler()
    session = make(connector, handler)
    session.start()
    await asyncio.wait_for(connector.connected.wait(), 1)
    first = connector.sessions[0]
    first.inbox.put_nowait(
        types.LiveServerMessage(
            session_resumption_update=types.LiveServerSessionResumptionUpdate(
                new_handle="handle-1", resumable=True
            )
        )
    )
    first.inbox.put_nowait(errors.APIError(1006, {"message": "Abnormal closure."}))
    for _ in range(50):
        await asyncio.sleep(0)
        if len(connector.sessions) == 2:
            break
    assert len(connector.sessions) == 2
    assert connector.configs[1].session_resumption is not None
    assert connector.configs[1].session_resumption.handle == "handle-1"
    assert ("lost", None) in handler.events
    assert session.running
    await session.stop()


async def test_go_away_triggers_immediate_resume() -> None:
    connector, handler = FakeConnector(), RecordingHandler()
    session = make(connector, handler)
    session.start()
    await asyncio.wait_for(connector.connected.wait(), 1)
    connector.sessions[0].inbox.put_nowait(
        types.LiveServerMessage(go_away=types.LiveServerGoAway(time_left="10s"))
    )
    for _ in range(50):
        await asyncio.sleep(0)
        if len(connector.sessions) == 2:
            break
    assert len(connector.sessions) == 2
    assert ("lost", None) not in handler.events
    await session.stop()


async def test_network_down_retries_without_crashing() -> None:
    connector, handler = FakeConnector(), RecordingHandler()
    connector.fail_with = [OSError("down"), OSError("down")]
    session = make(connector, handler)
    session.start()
    await asyncio.wait_for(connector.connected.wait(), 1)
    assert len(connector.configs) == 3
    await session.stop()


async def test_invalid_key_is_fatal_and_stops() -> None:
    connector, handler = FakeConnector(), RecordingHandler()
    connector.fail_with = [errors.APIError(1008, {"message": "API key not valid"})]
    session = make(connector, handler)
    session.start()
    await settle()
    assert handler.events == [("fatal", LiveErrorKind.INVALID_KEY)]
    assert not session.running


async def test_refused_setup_falls_back_to_next_model() -> None:
    connector, handler = FakeConnector(), RecordingHandler()
    connector.fail_with = [api_error(1008, "models/new-live is not found for API version v1beta")]
    session = LiveSession(connector, ["new-live", "old-live"], handler, lambda: "SYS")
    session.start()
    await asyncio.wait_for(connector.connected.wait(), 1)
    assert connector.models == ["new-live", "old-live"]
    assert session.model == "old-live"
    assert handler.events == []
    await session.stop()


async def test_refused_setup_with_no_fallback_reports_server_reason() -> None:
    connector, handler = FakeConnector(), RecordingHandler()
    reason = "Invalid JSON payload received. Unknown name \"foo\" at 'setup'"
    connector.fail_with = [api_error(1007, reason), api_error(1007, reason)]
    details: list[str] = []
    handler.on_fatal_error = lambda kind, detail: details.append(detail)  # type: ignore[method-assign]
    session = LiveSession(connector, ["a", "b"], handler, lambda: "SYS")
    session.start()
    await settle()
    assert connector.models == ["a", "b"]
    assert details == [reason]
    assert not session.running


def test_error_detail_trims_long_reasons() -> None:
    assert error_detail(api_error(1008, "short reason")) == "short reason"
    assert len(error_detail(api_error(1008, "x" * 500))) == 200
