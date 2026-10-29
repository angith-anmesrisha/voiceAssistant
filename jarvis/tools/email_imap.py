"""The Email Assistant: reads your inbox and saves reply drafts. It never sends.

It signs in over IMAP (encrypted) with an app password kept, like API keys, only in
environment variables: ``JARVIS_EMAIL_ADDRESS`` and ``JARVIS_EMAIL_PASSWORD`` (for Gmail
an "app password" from myaccount.google.com/apppasswords, with 2-step verification on),
plus ``JARVIS_EMAIL_IMAP`` for a server JARVIS can't guess from the address.

Reading uses read-only mode and ``BODY.PEEK``, so nothing is marked as read. A reply is
only ever *saved to your Drafts folder*, after you've seen it and said yes (medium
risk); you send it yourself from your mail app. There is no sending code in JARVIS.
Mail is outside text: what these tools return is screened by Prompt Guard, and mail
content and addresses are never logged.
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import imaplib
import re
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any

from loguru import logger

from jarvis.app.keys import get_key
from jarvis.tools.registry import Risk, ToolRegistry, ToolResult, ToolSpec
from jarvis.tools.writing import page_text

ADDRESS_ENV = "JARVIS_EMAIL_ADDRESS"
PASSWORD_ENV = "JARVIS_EMAIL_PASSWORD"
HOST_ENV = "JARVIS_EMAIL_IMAP"

_HOSTS = {
    "gmail.com": "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "outlook.com": "outlook.office365.com",
    "hotmail.com": "outlook.office365.com",
    "live.com": "outlook.office365.com",
    "yahoo.com": "imap.mail.yahoo.com",
    "icloud.com": "imap.mail.me.com",
    "me.com": "imap.mail.me.com",
    "zoho.com": "imap.zoho.com",
    "zohomail.in": "imap.zoho.in",
}
_DRAFT_NAMES = ("[Gmail]/Drafts", "Drafts", "INBOX.Drafts", "Draft")


class EmailError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Mail:
    uid: str
    sender: str
    sender_address: str
    to: str
    subject: str
    date: str
    text: str
    message_id: str
    references: str
    unread: bool

    def line(self, snippet: int = 240) -> str:
        flag = "unread · " if self.unread else ""
        preview = " ".join(self.text.split())[:snippet]
        return f"[{self.uid}] {flag}{self.date} · {self.sender}: {self.subject} | {preview}"


def guess_host(address: str) -> str:
    domain = address.rpartition("@")[2].lower()
    return _HOSTS.get(domain, f"imap.{domain}" if domain else "")


def _plain(message: Any) -> str:
    body = message.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    try:
        content = body.get_content()
    except (LookupError, ValueError):
        return ""
    if body.get_content_type() == "text/html":
        return page_text(content, limit=20_000)
    # Drop quoted earlier messages, which repeat the thread.
    lines = [line for line in content.splitlines() if not line.startswith(">")]
    return "\n".join(lines).strip()


def parse_mail(uid: str, raw: bytes, unread: bool) -> Mail:
    message = email.message_from_bytes(raw, policy=email.policy.default)
    name, address = parseaddr(str(message.get("From", "")))
    try:
        when = parsedate_to_datetime(str(message.get("Date", ""))).astimezone()
        date = f"{when:%d %b %H:%M}"
    except (TypeError, ValueError):
        date = ""
    return Mail(
        uid=uid,
        sender=name or address,
        sender_address=address,
        to=", ".join(a for _n, a in getaddresses([str(message.get("To", ""))])),
        subject=" ".join(str(message.get("Subject", "(no subject)")).split())[:200],
        date=date,
        text=_plain(message),
        message_id=str(message.get("Message-ID", "")).strip(),
        references=str(message.get("References", "")).strip(),
        unread=unread,
    )


def reply_draft(original: Mail, text: str, sender: str) -> EmailMessage:
    draft = EmailMessage()
    subject = original.subject
    draft["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    draft["From"] = sender
    draft["To"] = original.sender_address
    if original.message_id:
        draft["In-Reply-To"] = original.message_id
        draft["References"] = f"{original.references} {original.message_id}".strip()
    draft.set_content(text.strip() + "\n")
    return draft


def _quote(mailbox: str) -> str:
    return '"' + mailbox.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Mailbox:
    """One IMAP account. Every call opens a connection, does its job and logs out."""

    def __init__(
        self,
        address: str,
        password: str,
        host: str = "",
        *,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self.address = address
        self._password = password
        self.host = host or guess_host(address)
        self._connect = connect or (
            lambda host: imaplib.IMAP4_SSL(
                host, 993, ssl_context=ssl.create_default_context(), timeout=20
            )
        )

    @classmethod
    def from_environment(cls) -> Mailbox | None:
        address, password = get_key(ADDRESS_ENV), get_key(PASSWORD_ENV)
        if not address or not password:
            return None
        return cls(address, password, get_key(HOST_ENV) or "")

    def _session(self) -> Any:
        try:
            imap = self._connect(self.host)
            imap.login(self.address, self._password)
        except imaplib.IMAP4.error as exc:
            raise EmailError(
                "the mail server refused the sign-in (for Gmail use an app password)"
            ) from exc
        except OSError as exc:
            raise EmailError(f"couldn't reach {self.host}") from exc
        return imap

    def recent(self, count: int = 10, unread_only: bool = True, query: str = "") -> list[Mail]:
        imap = self._session()
        try:
            imap.select("INBOX", readonly=True)
            criteria = ["UNSEEN"] if unread_only else ["ALL"]
            words = re.sub(r'["\\\r\n]', " ", query).strip()
            if words:
                criteria += ["TEXT", _quote(words)]
            status, data = imap.uid("SEARCH", None, *criteria)
            if status != "OK":
                raise EmailError("the inbox couldn't be searched")
            uids = (data[0] or b"").split()[-max(1, min(count, 25)) :]
            unseen = set()
            if not unread_only and uids:
                _s, seen_data = imap.uid("SEARCH", None, "UNSEEN")
                unseen = set((seen_data[0] or b"").split())
            mails = [
                self._fetch(imap, uid.decode(), unread_only or uid in unseen)
                for uid in reversed(uids)
            ]
            return [m for m in mails if m is not None]
        finally:
            _logout(imap)

    def one(self, uid: str) -> Mail:
        imap = self._session()
        try:
            imap.select("INBOX", readonly=True)
            mail = self._fetch(imap, uid, False)
            if mail is None:
                raise EmailError(f"there's no message {uid} in the inbox")
            return mail
        finally:
            _logout(imap)

    def _fetch(self, imap: Any, uid: str, unread: bool) -> Mail | None:
        if not uid.isdigit():
            return None
        status, data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            return None
        return parse_mail(uid, data[0][1], unread)

    def save_draft(self, uid: str, text: str) -> str:
        """Save a reply to message ``uid`` in Drafts. Returns the Drafts folder name."""
        imap = self._session()
        try:
            imap.select("INBOX", readonly=True)
            original = self._fetch(imap, uid, False)
            if original is None:
                raise EmailError(f"there's no message {uid} in the inbox")
            folder = self._drafts(imap)
            draft = reply_draft(original, text, self.address)
            status, _ = imap.append(
                _quote(folder),
                r"(\Draft)",
                imaplib.Time2Internaldate(time.time()),
                draft.as_bytes(),
            )
            if status != "OK":
                raise EmailError("the server didn't accept the draft")
            return folder
        finally:
            _logout(imap)

    def _drafts(self, imap: Any) -> str:
        status, data = imap.list()
        names: list[str] = []
        for raw in data if status == "OK" else []:
            line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
            match = re.match(r'\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+"?(?P<name>.*?)"?$', line)
            if match is None:
                continue
            if "\\drafts" in match.group("flags").lower():
                return match.group("name")
            names.append(match.group("name"))
        for name in _DRAFT_NAMES:
            if name in names:
                return name
        return "Drafts"


def _logout(imap: Any) -> None:
    try:
        imap.logout()
    except Exception as exc:
        logger.debug("IMAP logout: {}", type(exc).__name__)


def register_email_tools(
    registry: ToolRegistry, mailbox: Callable[[], Mailbox | None] = Mailbox.from_environment
) -> None:
    def account() -> Mailbox:
        found = mailbox()
        if found is None:
            raise EmailError(
                "no mailbox is connected. Add your address and an app password in a Gmail "
                f"node's Connect card (or set {ADDRESS_ENV} and {PASSWORD_ENV})"
            )
        return found

    async def check_email(count: int = 10, unread_only: bool = True, query: str = "") -> ToolResult:
        try:
            box = account()
            mails = await asyncio.to_thread(box.recent, count, unread_only, query)
        except EmailError as exc:
            return ToolResult(False, f"Couldn't check email: {exc}.")
        if not mails:
            what = "unread " if unread_only else ""
            return ToolResult(True, f"No {what}messages{' matching that' if query else ''}.")
        return ToolResult(
            True,
            f"{len(mails)} {'unread ' if unread_only else ''}messages (newest first).",
            {"messages": [m.line() for m in mails]},
        )

    async def read_email(id: str) -> ToolResult:
        try:
            mail = await asyncio.to_thread(account().one, str(id))
        except EmailError as exc:
            return ToolResult(False, f"Couldn't read it: {exc}.")
        return ToolResult(
            True,
            f"Message {mail.uid} from {mail.sender}: {mail.subject}",
            {
                "from": f"{mail.sender} <{mail.sender_address}>",
                "to": mail.to,
                "date": mail.date,
                "subject": mail.subject,
                "text": mail.text[:6000],
            },
        )

    async def draft_reply(id: str, text: str) -> ToolResult:
        try:
            folder = await asyncio.to_thread(account().save_draft, str(id), text)
        except EmailError as exc:
            return ToolResult(False, f"Couldn't save the draft: {exc}.")
        return ToolResult(
            True, f"The reply is saved in {folder}, not sent. Send it from your mail app."
        )

    registry.register(
        ToolSpec(
            "check_email",
            "List recent inbox messages (unread only by default; query filters by words). "
            "Read-only. Use with read_email to summarise mail or find action items.",
            {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "minimum": 1, "maximum": 25},
                    "unread_only": {"type": "boolean"},
                    "query": {"type": "string", "maxLength": 100},
                },
                "required": [],
            },
            check_email,
            Risk.LOW,
            lambda a: "Check email",
            group="email",
        )
    )
    registry.register(
        ToolSpec(
            "read_email",
            "Read one inbox message in full by its id (from check_email). Read-only.",
            {
                "type": "object",
                "properties": {"id": {"type": "string", "maxLength": 20}},
                "required": ["id"],
            },
            read_email,
            Risk.LOW,
            lambda a: f"Read message {a.get('id', '')}",
            group="email",
        )
    )
    registry.register(
        ToolSpec(
            "draft_reply",
            "Save a reply to a message (id from check_email) in the user's Drafts folder. "
            "It is never sent; the user reviews and sends it. Write the full reply text.",
            {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "maxLength": 20},
                    "text": {"type": "string", "maxLength": 6000},
                },
                "required": ["id", "text"],
            },
            draft_reply,
            Risk.MEDIUM,
            lambda a: f"Save this reply to Drafts (not sent):\n{str(a.get('text', ''))[:600]}",
            group="email",
        )
    )
