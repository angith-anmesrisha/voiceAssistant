"""Gmail through Google's official Gmail API, signed in with OAuth 2.0 (read-only).

Owner, 2026-10-29: "Integrate the official Gmail API ... Use OAuth 2.0 and request the
minimum permissions necessary ... Start with read-only Gmail access. Never request my
Gmail password." JARVIS asks Google for one scope, ``gmail.readonly``: it can list and
read mail, and cannot send, delete, archive, label or mark anything read.

Sign-in is Google's flow for desktop apps: JARVIS opens Google's own sign-in page in the
browser (so JARVIS never sees the password), with PKCE and a random ``state``, and
listens once on ``http://127.0.0.1:<random port>`` for Google's answer. The refresh token
is kept in Windows Credential Manager (``JARVIS Gmail: <address>``), the short-lived
access token only in memory. Several accounts can be signed in. The OAuth client (made
once in the user's own Google Cloud project, type "Desktop app") is read from
``JARVIS_GOOGLE_CLIENT_ID`` / ``JARVIS_GOOGLE_CLIENT_SECRET`` like the API keys. Tokens,
codes and mail are never logged.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from email.utils import getaddresses, parseaddr
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from loguru import logger

from jarvis.app.keys import get_key
from jarvis.app.secrets import TokenStore
from jarvis.tools.email_imap import EmailError, Mail
from jarvis.tools.writing import page_text

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
CLIENT_ID_ENV = "JARVIS_GOOGLE_CLIENT_ID"
CLIENT_SECRET_ENV = "JARVIS_GOOGLE_CLIENT_SECRET"
SIGN_IN_TIMEOUT_S = 300.0

_DONE_PAGE = (
    "<!doctype html><meta charset=utf-8><title>JARVIS</title>"
    "<body style='font-family:sans-serif;background:#111;color:#ddd;padding:40px'>"
    "<h2>{title}</h2><p>{text}</p></body>"
)


class GmailError(EmailError):
    pass


def pkce() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge)."""
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def auth_url(client_id: str, redirect: str, challenge: str, state: str, hint: str = "") -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "access_type": "offline",  # a refresh token, so it keeps working
        "prompt": "consent select_account",
    }
    if hint:
        params["login_hint"] = hint
    return f"{AUTH_URL}?{urlencode(params)}"


class _Callback:
    """The one-shot loopback listener Google redirects the browser to."""

    def __init__(self, state: str) -> None:
        self._state = state
        self.result: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = int(self._server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1")
            target = line.split(" ")[1] if line.count(" ") >= 2 else "/"
            query = parse_qs(urlsplit(target).query)
            if "code" not in query and "error" not in query:
                await self._reply(writer, 404, "Not found", "")
                return
            if query.get("state", [""])[0] != self._state:
                await self._reply(
                    writer, 400, "Sign-in not accepted", "This answer wasn't for JARVIS."
                )
                return
            if "error" in query:
                await self._reply(writer, 200, "Gmail wasn't connected", "You can close this tab.")
                if not self.result.done():
                    self.result.set_exception(GmailError("Google sign-in was cancelled"))
                return
            await self._reply(
                writer, 200, "JARVIS is connected to Gmail", "Read-only. You can close this tab."
            )
            if not self.result.done():
                self.result.set_result(query["code"][0])
        except (OSError, TimeoutError, IndexError, UnicodeError):
            pass
        finally:
            writer.close()

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: int, title: str, text: str) -> None:
        body = _DONE_PAGE.format(title=title, text=text).encode()
        head = (
            f"HTTP/1.1 {status} OK\r\nContent-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        )
        writer.write(head.encode() + body)
        await writer.drain()


def _error_text(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return str(response.status_code)
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error.get("status") or response.status_code)[:160]
    if isinstance(error, str):
        return str(data.get("error_description") or error)[:160]
    return str(response.status_code)


def _decode(data: str) -> str:
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    return raw.decode("utf-8", errors="replace")


def _body(payload: dict[str, Any]) -> str:
    """The message text: the first text/plain part, else text/html made plain."""
    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime = str(part.get("mimeType", ""))
        data = (part.get("body") or {}).get("data")
        if data and not part.get("filename"):
            if mime == "text/plain":
                plain.append(_decode(str(data)))
            elif mime == "text/html":
                html.append(_decode(str(data)))
        for child in part.get("parts") or []:
            if isinstance(child, dict):
                walk(child)

    walk(payload)
    if plain:
        lines = [line for line in plain[0].splitlines() if not line.startswith(">")]
        return "\n".join(lines).strip()
    return page_text(html[0], limit=20_000) if html else ""


def parse_message(data: dict[str, Any], account: str = "") -> Mail:
    """A Gmail API ``users.messages.get`` (format=full) result as a Mail."""
    payload = data.get("payload") or {}
    headers = {
        str(h.get("name", "")).lower(): str(h.get("value", ""))
        for h in payload.get("headers") or []
        if isinstance(h, dict)
    }
    name, address = parseaddr(headers.get("from", ""))
    try:
        when = datetime.fromtimestamp(int(data.get("internalDate", 0)) / 1000).astimezone()
        date = f"{when:%d %b %H:%M}"
    except (TypeError, ValueError, OSError):
        date = ""
    message_id = str(data.get("id", ""))
    text = _body(payload) or str(data.get("snippet", ""))
    return Mail(
        uid=message_id,
        sender=name or address,
        sender_address=address,
        to=", ".join(a for _n, a in getaddresses([headers.get("to", "")])),
        subject=" ".join(headers.get("subject", "(no subject)").split())[:200],
        date=date,
        text=text,
        message_id=headers.get("message-id", "").strip(),
        references=headers.get("references", "").strip(),
        unread="UNREAD" in (data.get("labelIds") or []),
        account=account,
        link=f"https://mail.google.com/mail/?authuser={account}#all/{message_id}"
        if account
        else "",
    )


class GmailAccount:
    """One signed-in Gmail account (read-only)."""

    def __init__(
        self,
        address: str,
        refresh_token: str,
        client: httpx.AsyncClient,
        client_id: str,
        client_secret: str,
    ) -> None:
        self.address = address
        self._refresh = refresh_token
        self._client = client
        self._id, self._secret = client_id, client_secret
        self._access = ""
        self._expires = 0.0
        self.can_draft = False  # read-only scope: no drafts

    async def _token(self, *, force: bool = False) -> str:
        if self._access and not force and time.monotonic() < self._expires - 60:
            return self._access
        form = {
            "client_id": self._id,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh,
        }
        if self._secret:
            form["client_secret"] = self._secret
        try:
            response = await self._client.post(TOKEN_URL, data=form, timeout=20)
        except httpx.HTTPError as exc:
            raise GmailError(f"couldn't reach Google ({type(exc).__name__})") from None
        if response.status_code != 200:
            if "invalid_grant" in response.text:
                raise GmailError(
                    f"Google's sign-in for {self.address} has expired or was removed. Sign in "
                    "again in a Gmail node's Connect card"
                )
            raise GmailError(f"Google refused the sign-in: {_error_text(response)}")
        data = response.json()
        self._access = str(data.get("access_token", ""))
        self._expires = time.monotonic() + float(data.get("expires_in", 3600))
        return self._access

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        for attempt in range(2):
            token = await self._token(force=attempt > 0)
            try:
                response = await self._client.get(
                    f"{API}/{path}",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=20,
                )
            except httpx.HTTPError as exc:
                raise GmailError(f"couldn't reach Gmail ({type(exc).__name__})") from None
            if response.status_code == 401 and attempt == 0:
                continue
            if response.status_code != 200:
                raise GmailError(f"Gmail said: {_error_text(response)}")
            data = response.json()
            return data if isinstance(data, dict) else {}
        raise GmailError("Gmail didn't accept the sign-in")

    async def recent(
        self, count: int = 10, unread_only: bool = True, query: str = "", newer_than: str = ""
    ) -> list[Mail]:
        """Newest inbox messages first. ``newer_than`` is Gmail's, e.g. "1d"."""
        words = ["in:inbox"]
        if unread_only:
            words.append("is:unread")
        if newer_than:
            words.append(f"newer_than:{newer_than}")
        if query.strip():
            words.append(query.strip())
        found = await self._get(
            "messages", q=" ".join(words), maxResults=max(1, min(int(count), 50))
        )
        ids = [str(m["id"]) for m in found.get("messages") or [] if isinstance(m, dict)]
        gate = asyncio.Semaphore(5)

        async def fetch(message_id: str) -> Mail:
            async with gate:
                return await self.one(message_id)

        return list(await asyncio.gather(*(fetch(i) for i in ids)))

    async def one(self, message_id: str) -> Mail:
        if not message_id.isalnum():
            raise GmailError(f"there's no message {message_id}")
        data = await self._get(f"messages/{message_id}", format="full")
        return parse_message(data, self.address)


OpenUrl = Callable[[str], Any]


class GmailAccounts:
    """The signed-in Gmail accounts, and signing in or out."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        tokens: TokenStore,
        key: Callable[[str], str | None] = get_key,
    ) -> None:
        self._client = client
        self._tokens = tokens
        self._key = key
        self._open: dict[str, GmailAccount] = {}

    def client_ids(self) -> tuple[str, str]:
        return (self._key(CLIENT_ID_ENV) or "").strip(), (
            self._key(CLIENT_SECRET_ENV) or ""
        ).strip()

    @property
    def configured(self) -> bool:
        return bool(self.client_ids()[0])

    def addresses(self) -> list[str]:
        try:
            return self._tokens.accounts()
        except Exception as exc:  # Credential Manager unavailable
            logger.warning("Couldn't list Gmail sign-ins: {}", type(exc).__name__)
            return []

    def accounts(self) -> list[GmailAccount]:
        client_id, secret = self.client_ids()
        if not client_id:
            return []
        found = []
        for address in self.addresses():
            account = self._open.get(address)
            if account is None:
                token = self._tokens.get(address)
                if not token:
                    continue
                account = GmailAccount(address, token, self._client, client_id, secret)
                self._open[address] = account
            found.append(account)
        return found

    async def sign_in(
        self, open_url: OpenUrl, *, hint: str = "", timeout_s: float = SIGN_IN_TIMEOUT_S
    ) -> str:
        """Run Google's sign-in in the browser; returns the account's address."""
        client_id, secret = self.client_ids()
        if not client_id:
            raise GmailError("add the Google OAuth client ID first")
        verifier, challenge = pkce()
        state = secrets.token_urlsafe(24)
        callback = _Callback(state)
        await callback.start()
        redirect = f"http://127.0.0.1:{callback.port}"
        try:
            result = open_url(auth_url(client_id, redirect, challenge, state, hint))
            if isinstance(result, Awaitable):
                await result
            try:
                code = await asyncio.wait_for(callback.result, timeout_s)
            except TimeoutError:
                raise GmailError("the Google sign-in wasn't finished in time") from None
        finally:
            await callback.close()
        form = {
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect,
        }
        if secret:
            form["client_secret"] = secret
        try:
            response = await self._client.post(TOKEN_URL, data=form, timeout=20)
        except httpx.HTTPError as exc:
            raise GmailError(f"couldn't reach Google ({type(exc).__name__})") from None
        if response.status_code != 200:
            raise GmailError(f"Google didn't finish the sign-in: {_error_text(response)}")
        data = response.json()
        refresh = str(data.get("refresh_token", ""))
        granted = str(data.get("scope", ""))
        if not refresh:
            raise GmailError("Google gave no lasting sign-in; remove JARVIS's access and try again")
        if SCOPE not in granted.split():
            raise GmailError("Gmail read access wasn't allowed on Google's page")
        account = GmailAccount("", refresh, self._client, client_id, secret)
        account._access = str(data.get("access_token", ""))
        account._expires = time.monotonic() + float(data.get("expires_in", 3600))
        profile = await account._get("profile")
        address = str(profile.get("emailAddress", "")).strip().lower()
        if not address:
            raise GmailError("Gmail didn't say which account this is")
        account.address = address
        self._tokens.put(address, refresh)
        self._open[address] = account
        logger.info("Signed in to Gmail (read-only)")  # never the address or tokens
        return address

    async def remove(self, address: str) -> None:
        """Forget an account here and revoke JARVIS's access at Google (best effort)."""
        token = self._tokens.get(address)
        self._tokens.remove(address)
        self._open.pop(address.strip().lower(), None)
        if token:
            try:
                await self._client.post(REVOKE_URL, data={"token": token}, timeout=15)
            except httpx.HTTPError as exc:
                logger.info("Couldn't revoke a Gmail sign-in: {}", type(exc).__name__)
