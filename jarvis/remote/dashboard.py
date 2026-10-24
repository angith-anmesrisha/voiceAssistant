"""The phone dashboard: see what JARVIS is doing and send it requests from your phone.

Off until you pair a phone ("pair my phone", or Pair a phone… in the tray menu). That
starts a small web server on your home network and shows an address and a 6-digit code
that works once, for 10 minutes, with 5 tries. The phone then gets a long random token
in a cookie; only a hash of it is kept. After the first pairing the server starts with
JARVIS; "Unpair phones" in the tray forgets every phone and stops it.

Requests sent from the phone go through the same confirmations as typed ones, and those
are answered on the PC. The connection is plain HTTP inside your network, so use it on
networks you trust.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import secrets
import socket
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from loguru import logger

from jarvis.app.events import ChatEntry, EventBus
from jarvis.app.state import StateChanged
from jarvis.remote.page import PAGE
from jarvis.storage.keep import Keep

PAIR_SECONDS = 600
PAIR_TRIES = 5
_MAX_HEAD = 8192
_MAX_BODY = 8192
_COOKIE = "jarvis_phone"
_TOKENS = "dashboard_tokens"


def lan_address() -> str:
    """This PC's address on the local network (no packet is sent)."""
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 9))  # TEST-NET: only picks the outgoing interface
        return str(probe.getsockname()[0])
    return "127.0.0.1"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class _Pairing:
    code: str
    expires: float
    tries: int = 0


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    def cookie(self, name: str) -> str:
        for part in self.headers.get("cookie", "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return ""


@dataclass(frozen=True, slots=True)
class Response:
    status: HTTPStatus
    body: bytes
    content_type: str = "application/json"
    headers: tuple[tuple[str, str], ...] = ()


def _json(status: HTTPStatus, payload: dict[str, Any], *extra: tuple[str, str]) -> Response:
    return Response(status, json.dumps(payload).encode(), headers=extra)


class Dashboard:
    def __init__(
        self,
        bus: EventBus,
        keep: Keep,
        submit: Callable[[str], bool],
        *,
        port: int = 8765,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._keep = keep
        self._submit = submit
        self._port = port
        self._clock = clock
        self._server: asyncio.Server | None = None
        self._pairing: _Pairing | None = None
        self._entries: deque[dict[str, str]] = deque(maxlen=30)
        self._state = "idle"
        bus.subscribe(ChatEntry, self._on_entry)
        bus.subscribe(StateChanged, self._on_state)

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def address(self) -> str:
        return f"http://{lan_address()}:{self._port}"

    async def _on_entry(self, entry: ChatEntry) -> None:
        self._entries.append(
            {"kind": entry.kind.value, "text": entry.text[:2000], "mode": entry.mode or ""}
        )

    async def _on_state(self, event: StateChanged) -> None:
        self._state = event.current.value

    # --- lifecycle ---

    async def start_if_paired(self) -> None:
        if await self._tokens():
            await self.start()

    async def start(self) -> None:
        if self._server is None:
            self._server = await asyncio.start_server(self._serve, "0.0.0.0", self._port)
            logger.info("Phone dashboard listening on port {}", self._port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    async def pair(self) -> str:
        """Start (if needed) and return a fresh one-time code."""
        await self.start()
        code = f"{secrets.randbelow(1_000_000):06d}"
        self._pairing = _Pairing(code, self._clock() + PAIR_SECONDS)
        return code

    async def unpair_all(self) -> None:
        await self._keep.set_state(_TOKENS, "[]")
        self._pairing = None
        await self.stop()

    async def _tokens(self) -> list[str]:
        try:
            stored = json.loads(await self._keep.get_state(_TOKENS) or "[]")
        except ValueError:
            return []
        return [t for t in stored if isinstance(t, str)]

    async def _authorised(self, request: Request) -> bool:
        token = request.cookie(_COOKIE)
        if not token:
            return False
        digest = _digest(token)
        return any(hmac.compare_digest(digest, known) for known in await self._tokens())

    # --- HTTP ---

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(_read_request(reader), timeout=10)
            response = (
                await self.handle(request)
                if request
                else _json(HTTPStatus.BAD_REQUEST, {"error": "bad request"})
            )
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            response = _json(HTTPStatus.REQUEST_TIMEOUT, {"error": "timeout"})
        except Exception as exc:
            logger.warning("Dashboard request failed: {}", type(exc).__name__)
            response = _json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "failed"})
        with contextlib.suppress(Exception):
            writer.write(_encode(response))
            await writer.drain()
            writer.close()

    async def handle(self, request: Request) -> Response:
        if request.method == "GET" and request.path == "/":
            return Response(HTTPStatus.OK, PAGE.encode(), "text/html; charset=utf-8")
        if request.method != "POST" and request.path != "/state":
            return _json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        if request.method == "POST" and request.headers.get("x-jarvis") != "1":
            return _json(HTTPStatus.FORBIDDEN, {"error": "missing header"})  # blocks forms
        if request.path == "/pair" and request.method == "POST":
            return await self._pair(request)
        if not await self._authorised(request):
            return _json(HTTPStatus.UNAUTHORIZED, {"error": "pair this phone first"})
        if request.path == "/state" and request.method == "GET":
            return _json(
                HTTPStatus.OK, {"state": self._state, "entries": list(self._entries)[-20:]}
            )
        if request.path == "/ask" and request.method == "POST":
            text = str(_body(request).get("text", "")).strip()[:2000]
            if not text:
                return _json(HTTPStatus.BAD_REQUEST, {"error": "empty"})
            accepted = self._submit(text)
            return _json(HTTPStatus.OK, {"ok": accepted, "busy": not accepted})
        if request.path == "/unpair" and request.method == "POST":
            digest = _digest(request.cookie(_COOKIE))
            kept = [t for t in await self._tokens() if not hmac.compare_digest(t, digest)]
            await self._keep.set_state(_TOKENS, json.dumps(kept))
            return _json(HTTPStatus.OK, {"ok": True}, ("Set-Cookie", f"{_COOKIE}=; Max-Age=0"))
        return _json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    async def _pair(self, request: Request) -> Response:
        pairing = self._pairing
        code = str(_body(request).get("code", "")).strip()
        if pairing is None or self._clock() > pairing.expires:
            return _json(HTTPStatus.FORBIDDEN, {"error": "Ask JARVIS to pair a phone first."})
        pairing.tries += 1
        if not hmac.compare_digest(code, pairing.code):
            if pairing.tries >= PAIR_TRIES:
                self._pairing = None
            return _json(HTTPStatus.FORBIDDEN, {"error": "That code isn't right."})
        self._pairing = None  # one use
        token = secrets.token_urlsafe(32)
        tokens = [*await self._tokens(), _digest(token)][-5:]
        await self._keep.set_state(_TOKENS, json.dumps(tokens))
        cookie = f"{_COOKIE}={token}; Max-Age=31536000; HttpOnly; SameSite=Strict; Path=/"
        return _json(HTTPStatus.OK, {"ok": True}, ("Set-Cookie", cookie))


def _body(request: Request) -> dict[str, Any]:
    try:
        value = json.loads(request.body or b"{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


async def _read_request(reader: asyncio.StreamReader) -> Request | None:
    head = await reader.readuntil(b"\r\n\r\n")
    if len(head) > _MAX_HEAD:
        return None
    lines = head.decode("latin-1").split("\r\n")
    try:
        method, target, _version = lines[0].split(" ", 2)
    except ValueError:
        return None
    headers = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    try:
        length = int(headers.get("content-length", "0"))
    except ValueError:
        return None
    if length < 0 or length > _MAX_BODY:
        return None
    body = await reader.readexactly(length) if length else b""
    return Request(method.upper(), target.split("?", 1)[0], headers, body)


def _encode(response: Response) -> bytes:
    head = [
        f"HTTP/1.1 {response.status.value} {response.status.phrase}",
        f"Content-Type: {response.content_type}",
        f"Content-Length: {len(response.body)}",
        "Cache-Control: no-store",
        "X-Content-Type-Options: nosniff",
        "X-Frame-Options: DENY",
        "Content-Security-Policy: default-src 'self'; script-src 'unsafe-inline'; "
        "style-src 'unsafe-inline'",
        "Connection: close",
        *(f"{key}: {value}" for key, value in response.headers),
    ]
    return ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + response.body
