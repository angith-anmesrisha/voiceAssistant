"""The Email Assistant: reads your inbox and saves reply drafts. It never sends.

Gmail accounts signed in with Google (``gmail_api.py``: the official Gmail API, OAuth 2.0,
read-only) come first, and several can be signed in; their message ids are "<n>:<id>"
when there's more than one. Read-only Gmail can't hold drafts, so ``draft_reply`` then
hands the reply back to copy. With no Gmail sign-in, the older IMAP mailbox below is used.

The IMAP mailbox signs in (encrypted) with an app password kept, like API keys, only in
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
from typing import Any, Protocol

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
    account: str = ""  # which signed-in mailbox it's in (Gmail API)
    link: str = ""  # opens it in Gmail

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


class MailSource(Protocol):
    """A mailbox the email tools read: a Gmail API account, or an IMAP one."""

    address: str
    can_draft: bool

    async def recent(
        self, count: int = 10, unread_only: bool = True, query: str = "", newer_than: str = ""
    ) -> list[Mail]: ...
    async def one(self, message_id: str) -> Mail: ...


class ImapSource:
    """The IMAP mailbox (app password, from the environment) as a MailSource."""

    can_draft = True

    def __init__(self, box: Mailbox) -> None:
        self.box = box
        self.address = box.address

    async def recent(
        self, count: int = 10, unread_only: bool = True, query: str = "", newer_than: str = ""
    ) -> list[Mail]:
        return await asyncio.to_thread(self.box.recent, count, unread_only, query)

    async def one(self, message_id: str) -> Mail:
        return await asyncio.to_thread(self.box.one, message_id)


Sources = Callable[[], list[MailSource]]


def mail_sources(
    gmail: Any = None, mailbox: Callable[[], Mailbox | None] = Mailbox.from_environment
) -> Sources:
    """Signed-in Gmail accounts (official API, read-only) first; with none, the IMAP
    mailbox if one is set up in the environment."""

    def sources() -> list[MailSource]:
        found: list[MailSource] = list(gmail.accounts()) if gmail is not None else []
        if not found:
            box = mailbox()
            if box is not None:
                found.append(ImapSource(box))
        return found

    return sources


NOT_CONNECTED = (
    "no Gmail account is connected. Sign in with Google in a Gmail node's Connect card (read-only)"
)


def register_email_tools(
    registry: ToolRegistry,
    mailbox: Callable[[], Mailbox | None] = Mailbox.from_environment,
    *,
    sources: Sources | None = None,
) -> None:
    listed = sources or mail_sources(None, mailbox)

    def accounts() -> list[MailSource]:
        found = listed()
        if not found:
            raise EmailError(NOT_CONNECTED)
        return found

    def split_id(raw: str, found: list[MailSource]) -> tuple[MailSource, str]:
        number, colon, rest = raw.strip().partition(":")
        if colon and number.isdigit() and 1 <= int(number) <= len(found):
            return found[int(number) - 1], rest
        return found[0], raw.strip()

    async def check_email(
        count: int = 10, unread_only: bool = True, query: str = "", account: str = ""
    ) -> ToolResult:
        try:
            found = accounts()
        except EmailError as exc:
            return ToolResult(False, f"Couldn't check email: {exc}.")
        many = len(found) > 1
        picked = [
            (n, s) for n, s in enumerate(found, 1) if not account or account.lower() in s.address
        ] or list(enumerate(found, 1))
        results = await asyncio.gather(
            *(s.recent(count, unread_only, query) for _n, s in picked), return_exceptions=True
        )
        lines: list[str] = []
        problems: list[str] = []
        for (n, source), result in zip(picked, results, strict=True):
            if isinstance(result, EmailError):
                problems.append(f"{source.address}: {result}" if many else str(result))
                continue
            if isinstance(result, BaseException):
                problems.append(f"{source.address}: {type(result).__name__}")
                continue
            for mail in result:
                line = mail.line()
                if many:
                    line = line.replace(f"[{mail.uid}]", f"[{n}:{mail.uid}] {source.address} ·", 1)
                lines.append(line)
        if problems and not lines and len(problems) == len(picked):
            return ToolResult(False, f"Couldn't check email: {'; '.join(problems)}.")
        what = "unread " if unread_only else ""
        note = f" Couldn't check {'; '.join(problems)}." if problems else ""
        if not lines:
            return ToolResult(True, f"No {what}messages{' matching that' if query else ''}.{note}")
        return ToolResult(
            True,
            f"{len(lines)} {what}messages (newest first per account).{note}",
            {"messages": lines[: max(count, 1) * len(picked)]},
        )

    async def read_email(id: str) -> ToolResult:
        try:
            found = accounts()
            source, message_id = split_id(str(id), found)
            mail = await source.one(message_id)
        except EmailError as exc:
            return ToolResult(False, f"Couldn't read it: {exc}.")
        data = {
            "from": f"{mail.sender} <{mail.sender_address}>",
            "to": mail.to,
            "date": mail.date,
            "subject": mail.subject,
            "text": mail.text[:6000],
        }
        if mail.link:
            data["link"] = mail.link
        return ToolResult(True, f"Message {id} from {mail.sender}: {mail.subject}", data)

    async def draft_reply(id: str, text: str) -> ToolResult:
        try:
            found = accounts()
            source, message_id = split_id(str(id), found)
            if not isinstance(source, ImapSource):
                return ToolResult(
                    True,
                    "JARVIS only has read-only Gmail access, so the reply wasn't saved in "
                    "Gmail. Here it is to copy into your reply.",
                    {"reply": text.strip()},
                )
            folder = await asyncio.to_thread(source.box.save_draft, message_id, text)
        except EmailError as exc:
            return ToolResult(False, f"Couldn't save the draft: {exc}.")
        return ToolResult(
            True, f"The reply is saved in {folder}, not sent. Send it from your mail app."
        )

    registry.register(
        ToolSpec(
            "check_email",
            "List recent inbox messages (unread only by default; query filters by words; "
            "account picks one of several signed-in addresses). "
            "Read-only. Use with read_email to summarise mail or find action items.",
            {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "minimum": 1, "maximum": 25},
                    "unread_only": {"type": "boolean"},
                    "query": {"type": "string", "maxLength": 100},
                    "account": {"type": "string", "maxLength": 120},
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
                "properties": {"id": {"type": "string", "maxLength": 40}},
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
                    "id": {"type": "string", "maxLength": 40},
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
