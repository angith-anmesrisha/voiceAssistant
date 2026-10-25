"""The Email Assistant: read-only inbox access, replies saved to Drafts after a yes, and
nothing ever sent."""

from __future__ import annotations

import imaplib
from email.message import EmailMessage
from typing import Any

import pytest

from jarvis.tools import email_imap
from jarvis.tools.email_imap import EmailError, Mailbox, guess_host, register_email_tools
from jarvis.tools.registry import Risk, ToolRegistry


def message(uid: int, sender: str, subject: str, body: str, html: bool = False) -> bytes:
    mail = EmailMessage()
    mail["From"] = sender
    mail["To"] = "me@gmail.com"
    mail["Subject"] = subject
    mail["Date"] = "Sat, 24 Oct 2026 09:15:00 +0530"
    mail["Message-ID"] = f"<m{uid}@example.com>"
    if html:
        mail.set_content(f"<html><body><p>{body}</p></body></html>", subtype="html")
    else:
        mail.set_content(body + "\n> quoted older message")
    return mail.as_bytes()


class FakeImap:
    def __init__(self, store: dict[str, bytes], unseen: set[str], *, refuse: bool = False):
        self.store = store
        self.unseen = unseen
        self.refuse = refuse
        self.commands: list[tuple[Any, ...]] = []
        self.appended: list[tuple[str, str, bytes]] = []
        self.readonly: bool | None = None

    def login(self, user: str, password: str) -> None:
        if self.refuse:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.readonly = readonly
        return "OK", [b"3"]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        self.commands.append((command, *args))
        if command == "SEARCH":
            pool = self.unseen if "UNSEEN" in args else set(self.store)
            if "TEXT" in args:
                words = args[args.index("TEXT") + 1].strip('"').lower()
                pool = {u for u in pool if words in self.store[u].decode().lower()}
            return "OK", [" ".join(sorted(pool, key=int)).encode()]
        if command == "FETCH":
            assert args[1] == "(BODY.PEEK[])"  # never marks anything read
            uid = args[0]
            if uid not in self.store:
                return "OK", [None]
            return "OK", [(f"{uid} (UID {uid} BODY[] {{1}}".encode(), self.store[uid]), b")"]
        raise AssertionError(f"unexpected {command}")

    def list(self) -> tuple[str, list[bytes]]:
        return "OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Drafts"',
        ]

    def append(self, mailbox: str, flags: str, when: str, data: bytes) -> tuple[str, Any]:
        self.appended.append((mailbox, flags, data))
        return "OK", [b"APPEND completed"]

    def logout(self) -> None:
        pass


@pytest.fixture
def imap() -> FakeImap:
    return FakeImap(
        {
            "7": message(
                7, "Priya <priya@work.com>", "Budget", "Please send the budget by Friday."
            ),
            "8": message(
                8,
                "News <news@site.com>",
                "Weekly digest",
                "Top stories from this week in tech and science",
                html=True,
            ),
            "9": message(9, "Arun <arun@work.com>", "Lunch?", "Lunch on Monday?"),
        },
        unseen={"7", "9"},
    )


def tools(imap: FakeImap) -> ToolRegistry:
    registry = ToolRegistry()
    box = Mailbox("me@gmail.com", "app-password", connect=lambda host: imap)
    register_email_tools(registry, lambda: box)
    return registry


async def call(registry: ToolRegistry, name: str, **args: Any) -> Any:
    spec = registry.get(name)
    assert spec is not None
    return await registry.run(spec, args)


def test_hosts_are_guessed_from_the_address() -> None:
    assert guess_host("a@gmail.com") == "imap.gmail.com"
    assert guess_host("a@outlook.com") == "outlook.office365.com"
    assert guess_host("a@mycompany.in") == "imap.mycompany.in"


async def test_check_email_lists_unread_newest_first_read_only(imap: FakeImap) -> None:
    result = await call(tools(imap), "check_email")
    assert result.ok
    lines = result.data["messages"]
    assert lines[0].startswith("[9] unread · 24 Oct") and "Arun: Lunch?" in lines[0]
    assert "Priya: Budget | Please send the budget by Friday." in lines[1]
    assert "quoted older message" not in lines[1]
    assert imap.readonly is True


async def test_search_and_html_mail(imap: FakeImap) -> None:
    result = await call(tools(imap), "check_email", unread_only=False, query="digest")
    [line] = result.data["messages"]
    assert line.startswith("[8] 24 Oct") and "Top stories" in line


async def test_read_email(imap: FakeImap) -> None:
    result = await call(tools(imap), "read_email", id="7")
    assert result.data["from"] == "Priya <priya@work.com>"
    assert result.data["text"] == "Please send the budget by Friday."
    missing = await call(tools(imap), "read_email", id="99")
    assert not missing.ok


async def test_a_reply_is_only_saved_as_a_draft(imap: FakeImap) -> None:
    registry = tools(imap)
    spec = registry.get("draft_reply")
    assert spec is not None and spec.risk_for({}) is Risk.MEDIUM
    result = await call(registry, "draft_reply", id="7", text="Hi Priya, sending it Thursday.")
    assert result.ok and "not sent" in result.summary
    [(folder, flags, data)] = imap.appended
    assert folder == '"[Gmail]/Drafts"' and flags == r"(\Draft)"
    text = data.decode()
    assert "Subject: Re: Budget" in text and "To: priya@work.com" in text
    assert "In-Reply-To: <m7@example.com>" in text
    assert not hasattr(email_imap, "smtplib")  # there is no sending code


async def test_sign_in_problems_and_no_account() -> None:
    refused = FakeImap({}, set(), refuse=True)
    result = await call(tools(refused), "check_email")
    assert not result.ok and "app password" in result.summary
    registry = ToolRegistry()
    register_email_tools(registry, lambda: None)
    nothing = await call(registry, "check_email")
    assert not nothing.ok and "JARVIS_EMAIL_ADDRESS" in nothing.summary


def test_mailbox_errors_are_email_errors() -> None:
    box = Mailbox("me@gmail.com", "x", connect=lambda host: (_ for _ in ()).throw(OSError()))
    with pytest.raises(EmailError):
        box.recent()
