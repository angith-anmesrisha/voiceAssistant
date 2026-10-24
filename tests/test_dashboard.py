"""The phone dashboard: off until paired, one-time codes, a token cookie, plain text."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import httpx
import pytest

from jarvis.agent.commands import pair_command
from jarvis.app.events import ChatEntry, EntryKind, EventBus
from jarvis.remote.dashboard import Dashboard, Request, Response
from jarvis.storage.keep import Keep


def make(tmp_path: Path, clock: list[float] | None = None) -> tuple[Dashboard, list[str], EventBus]:
    asked: list[str] = []
    bus = EventBus()
    now = clock or [1000.0]

    def submit(text: str) -> bool:
        asked.append(text)
        return True

    board = Dashboard(bus, Keep(tmp_path / "keep.sqlite3"), submit, port=0, clock=lambda: now[0])
    return board, asked, bus


def post(path: str, body: dict[str, object], cookie: str = "", header: bool = True) -> Request:
    headers = {"x-jarvis": "1"} if header else {}
    if cookie:
        headers["cookie"] = cookie
    return Request("POST", path, headers, json.dumps(body).encode())


def cookie_of(response: Response) -> str:
    for key, value in response.headers:
        if key == "Set-Cookie":
            return value.split(";")[0]
    return ""


@pytest.mark.parametrize(
    ("text", "expected"),
    [("pair my phone", True), ("Jarvis, connect my phone to you", True),
     ("set up the phone dashboard", True), ("call my phone", False),
     ("pair my phone with the speaker in the kitchen", False)],
)  # fmt: skip
def test_pair_command(text: str, expected: bool) -> None:
    assert pair_command(text) is expected


async def test_nothing_without_pairing(tmp_path: Path) -> None:
    board, asked, _bus = make(tmp_path)
    page = await board.handle(Request("GET", "/", {}, b""))
    assert page.status == 200 and b"<title>JARVIS</title>" in page.body
    assert (await board.handle(Request("GET", "/state", {}, b""))).status == 401
    assert (await board.handle(post("/ask", {"text": "hi"}))).status == 401
    assert (await board.handle(post("/pair", {"code": "123456"}))).status == 403
    assert asked == [] and not board.running


async def test_pairing_then_asking(tmp_path: Path) -> None:
    clock = [1000.0]
    board, asked, bus = make(tmp_path, clock)
    code = await board.pair()
    try:
        assert board.running and len(code) == 6
        assert (await board.handle(post("/pair", {"code": code}, header=False))).status == 403
        wrong = "000000" if code != "000000" else "111111"
        assert (await board.handle(post("/pair", {"code": wrong}))).status == 403
        paired = await board.handle(post("/pair", {"code": code}))
        cookie = cookie_of(paired)
        assert paired.status == 200 and cookie.startswith("jarvis_phone=")
        assert (await board.handle(post("/pair", {"code": code}))).status == 403  # once only

        await bus.publish(ChatEntry(EntryKind.ASSISTANT, "<b>Hello</b>", mode="briefing"))
        state = await board.handle(Request("GET", "/state", {"cookie": cookie}, b""))
        data = json.loads(state.body)
        assert data["entries"][-1] == {
            "kind": "assistant",
            "text": "<b>Hello</b>",
            "mode": "briefing",
        }
        sent = await board.handle(post("/ask", {"text": "what's the weather"}, cookie))
        assert json.loads(sent.body)["ok"] and asked == ["what's the weather"]
        stolen = "jarvis_phone=" + "x" * 43
        assert (await board.handle(post("/ask", {"text": "hi"}, stolen))).status == 401

        # A later JARVIS start brings the server back for the paired phone.
        await board.stop()
        await board.start_if_paired()
        assert board.running
        await board.unpair_all()
        assert not board.running
        assert (await board.handle(Request("GET", "/state", {"cookie": cookie}, b""))).status == 401
    finally:
        await board.stop()


async def test_codes_expire_and_run_out_of_tries(tmp_path: Path) -> None:
    clock = [1000.0]
    board, _asked, _bus = make(tmp_path, clock)
    try:
        code = await board.pair()
        clock[0] += 601
        assert (await board.handle(post("/pair", {"code": code}))).status == 403
        code = await board.pair()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            await board.handle(post("/pair", {"code": wrong}))
        assert (await board.handle(post("/pair", {"code": code}))).status == 403
    finally:
        await board.stop()


async def test_over_real_http(tmp_path: Path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    asked: list[str] = []

    def submit(text: str) -> bool:
        asked.append(text)
        return True

    board = Dashboard(EventBus(), Keep(tmp_path / "k.sqlite3"), submit, port=port)
    code = await board.pair()
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            assert (await client.get("/")).headers["x-frame-options"] == "DENY"
            paired = await client.post("/pair", json={"code": code}, headers={"X-Jarvis": "1"})
            assert paired.status_code == 200
            sent = await client.post("/ask", json={"text": "hello"}, headers={"X-Jarvis": "1"})
            assert sent.json() == {"ok": True, "busy": False} and asked == ["hello"]
            huge = await client.post("/ask", content=b"x" * 9000, headers={"X-Jarvis": "1"})
            assert huge.status_code == 400
    finally:
        await board.stop()
