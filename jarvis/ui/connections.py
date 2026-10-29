"""One-time setup for the services workflow nodes use (owner, 2026-10-26: "the gmail
node where in i should be able to add in my credentials to connect gmail (only once for
setup)").

Gmail, Calendar and Telegram settings are saved as Windows user environment variables
(the same as ``setx``, like the API keys), so the tools read them exactly as before.
Logins for the "Fill in a login" node go to Windows Credential Manager. Secret fields are
masked, are never shown again once saved (leave one blank to keep it), and are never
logged or written to workflows.json.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from loguru import logger
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from jarvis.app.keys import get_key, save_key
from jarvis.app.secrets import Login, LoginStore, MemoryLogins

if TYPE_CHECKING:
    from jarvis.tools.gmail_api import GmailAccounts


@dataclass(frozen=True)
class Field:
    key: str  # the environment variable it's saved as
    label: str
    secret: bool = False
    required: bool = True
    hint: str = ""


@dataclass(frozen=True)
class Service:
    name: str
    title: str
    fields: tuple[Field, ...]
    help: str


SERVICES: dict[str, Service] = {
    "gmail": Service(
        "gmail",
        "Gmail",
        (
            Field(
                "JARVIS_GOOGLE_CLIENT_ID",
                "Google OAuth client ID",
                hint="…apps.googleusercontent.com (Desktop app client)",
            ),
            Field(
                "JARVIS_GOOGLE_CLIENT_SECRET",
                "Client secret",
                secret=True,
                hint="from the same Desktop app client",
            ),
        ),
        "Signs in with Google's own page (JARVIS never sees your password) and asks only "
        "for read-only Gmail access: it can't send, delete, archive or change mail. Once: "
        "make a Desktop app OAuth client in your Google Cloud project with the Gmail API "
        "on, paste its ID and secret, then press Sign in with Google for each account.",
    ),
    "zai": Service(
        "zai",
        "Z.ai",
        (Field("ZAI_API_KEY", "Z.ai API key", secret=True, hint="from z.ai > API keys"),),
        "Z.ai writes the morning brief. The email excerpts, tasks, news and prices in it "
        "are sent to Z.ai, an external service, and processed there under Z.ai's terms. "
        "Only what the summary needs is sent: sender, subject, time and the start of each "
        "email, never your passwords or Google sign-in.",
    ),
    "calendar": Service(
        "calendar",
        "Calendar",
        (
            Field(
                "JARVIS_CALENDAR_ICS",
                "Secret iCal address",
                secret=True,
                hint="https://calendar.google.com/calendar/ical/…/basic.ics",
            ),
        ),
        "Google Calendar: Settings > your calendar > Integrate calendar > Secret address "
        "in iCal format. Outlook: Settings > Shared calendars > Publish. Read-only.",
    ),
    "telegram": Service(
        "telegram",
        "Telegram",
        (
            Field(
                "TELEGRAM_BOT_TOKEN", "Bot token", secret=True, hint="123456:ABC… from @BotFather"
            ),
            Field("TELEGRAM_CHAT_ID", "Your chat", hint="press Find my chat"),
        ),
        "In Telegram, message @BotFather, send /newbot and copy the token. Then send your "
        "new bot any message and press Find my chat. Every send asks you first.",
    ),
}

FindChat = Callable[[str], Awaitable[tuple[str, str] | None]]


class Connections:
    """Whether each service is set up, and saving it once. ``get``/``save`` default to
    the user environment; tests pass their own."""

    def __init__(
        self,
        logins: LoginStore | None = None,
        *,
        get: Callable[[str], str | None] = get_key,
        save: Callable[[str, str], bool] = save_key,
        find_chat: FindChat | None = None,
        gmail: GmailAccounts | None = None,
        open_url: Callable[[str], object] | None = None,
    ) -> None:
        self.logins = logins if logins is not None else MemoryLogins()
        self._get, self._save = get, save
        self.find_chat = find_chat
        self.gmail = gmail
        self.open_url = open_url

    def value(self, key: str) -> str:
        return (self._get(key) or "").strip()

    def connected(self, service: str) -> bool:
        if service == "logins":
            return bool(self.login_names())
        if service == "gmail":
            return bool(self.gmail_accounts()) or bool(self.value("JARVIS_EMAIL_PASSWORD"))
        spec = SERVICES.get(service)
        return spec is not None and all(self.value(f.key) for f in spec.fields if f.required)

    def status(self, service: str) -> str:
        if service == "logins":
            count = len(self.login_names())
            if not count:
                return "No logins saved yet"
            return f"{count} login{'' if count == 1 else 's'} saved in Credential Manager"
        if not self.connected(service):
            return "Not set up yet"
        if service == "gmail":
            accounts = self.gmail_accounts()
            if accounts:
                return "Signed in (read-only): " + ", ".join(accounts)
            return f"Connected with an app password as {self.value('JARVIS_EMAIL_ADDRESS')}"
        return "Connected"

    def gmail_accounts(self) -> list[str]:
        if self.gmail is None or not self.value("JARVIS_GOOGLE_CLIENT_ID"):
            return []
        return self.gmail.addresses()

    def save(self, service: str, values: dict[str, str]) -> bool:
        """Save the filled-in fields (blank ones keep what's saved)."""
        spec = SERVICES[service]
        ok = True
        for field in spec.fields:
            value = values.get(field.key, "").strip()
            if value:
                ok = self._save(field.key, value) and ok
        logger.info("Saved the {} connection", service)  # never the values
        return ok

    def login_names(self) -> list[str]:
        try:
            return [login.name for login in self.logins.all()]
        except Exception as exc:  # Credential Manager unavailable
            logger.warning("Couldn't list saved logins: {}", type(exc).__name__)
            return []


class ConnectDialog(QDialog):
    """Set up Gmail, Calendar or Telegram."""

    def __init__(self, parent: QWidget | None, connections: Connections, service: str) -> None:
        super().__init__(parent)
        self._connections = connections
        self._spec = SERVICES[service]
        self._finding: asyncio.Future[None] | None = None
        self.setWindowTitle(f"Connect {self._spec.title}")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        heading = QLabel(f"Connect {self._spec.title}")
        heading.setObjectName("title")
        layout.addWidget(heading)
        about = QLabel(self._spec.help)
        about.setWordWrap(True)
        about.setObjectName("hint")
        layout.addWidget(about)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.inputs: dict[str, QLineEdit] = {}
        for field in self._spec.fields:
            box = QLineEdit()
            saved = connections.value(field.key)
            if field.secret:
                box.setEchoMode(QLineEdit.EchoMode.Password)
                box.setPlaceholderText("Saved; leave blank to keep it" if saved else field.hint)
            else:
                box.setText(saved)
                box.setPlaceholderText(field.hint)
            self.inputs[field.key] = box
            label = QLabel(field.label.upper())
            label.setObjectName("section")
            if field.key == "TELEGRAM_CHAT_ID":
                row = QWidget()
                line = QHBoxLayout(row)
                line.setContentsMargins(0, 0, 0, 0)
                line.addWidget(box, 1)
                self.find_button = QPushButton("Find my chat")
                self.find_button.clicked.connect(self._find_chat)
                self.find_button.setEnabled(connections.find_chat is not None)
                line.addWidget(self.find_button)
                form.addRow(label, row)
            else:
                form.addRow(label, box)
        layout.addLayout(form)
        self.note = QLabel("Saved for your Windows account only. JARVIS never logs these.")
        self.note.setWordWrap(True)
        self.note.setObjectName("hint")
        layout.addWidget(self.note)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.save_button = QPushButton("Save")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self.save)
        buttons.addWidget(cancel)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)

    def values(self) -> dict[str, str]:
        return {key: box.text() for key, box in self.inputs.items()}

    def save(self) -> None:
        values = self.values()
        missing = [
            f.label
            for f in self._spec.fields
            if f.required and not values[f.key].strip() and not self._connections.value(f.key)
        ]
        if missing:
            self.note.setText("Still needed: " + ", ".join(missing) + ".")
            self.note.setObjectName("problem")
            self.note.setStyleSheet("")
            return
        if not self._connections.save(self._spec.name, values):
            self.note.setText("Saved for now, but Windows didn't keep it for next time.")
        self.accept()

    def _find_chat(self) -> None:
        find = self._connections.find_chat
        token = self.inputs["TELEGRAM_BOT_TOKEN"].text().strip() or self._connections.value(
            "TELEGRAM_BOT_TOKEN"
        )
        if find is None or not token:
            self.note.setText("Paste the bot token first.")
            return
        self.note.setText("Looking for your message to the bot…")

        async def go() -> None:
            try:
                found = await find(token)
            except Exception as exc:
                self.note.setText(f"Couldn't ask Telegram: {exc}")
                return
            if found is None:
                self.note.setText("No messages yet. Send your bot a message, then try again.")
                return
            chat, name = found
            self.inputs["TELEGRAM_CHAT_ID"].setText(chat)
            self.note.setText(f"Found the chat with {name or chat}.")

        try:
            self._finding = asyncio.ensure_future(go())
        except RuntimeError:  # no running event loop (tests)
            self.note.setText("Telegram can't be reached right now.")


class GmailDialog(ConnectDialog):
    """Gmail: the OAuth client once, then "Sign in with Google" per account (read-only).
    Accounts can be removed, which also revokes JARVIS's access at Google."""

    def __init__(self, parent: QWidget | None, connections: Connections) -> None:
        super().__init__(parent, connections, "gmail")
        self._busy: asyncio.Future[None] | None = None
        layout = self.layout()
        assert isinstance(layout, QVBoxLayout)
        label = QLabel("SIGNED-IN ACCOUNTS")
        label.setObjectName("section")
        self.accounts = QListWidget()
        self.accounts.setFixedHeight(84)
        row = QHBoxLayout()
        self.sign_in_button = QPushButton("Sign in with Google")
        self.sign_in_button.setObjectName("primary")
        self.sign_in_button.clicked.connect(self.sign_in)
        self.remove_button = QPushButton("Remove selected")
        self.remove_button.setObjectName("danger")
        self.remove_button.clicked.connect(self.remove_selected)
        row.addWidget(self.sign_in_button)
        row.addStretch(1)
        row.addWidget(self.remove_button)
        at = layout.indexOf(self.note)
        layout.insertWidget(at, label)
        layout.insertWidget(at + 1, self.accounts)
        layout.insertLayout(at + 2, row)
        self.save_button.setText("Done")
        self.note.setText(
            "The sign-in is kept in Windows Credential Manager. JARVIS never logs it, and "
            "never sends, deletes, archives or changes mail."
        )
        self.refresh()

    def refresh(self) -> None:
        self.accounts.clear()
        for address in self._connections.gmail_accounts():
            self.accounts.addItem(address)

    def _save_client(self) -> bool:
        values = self.values()
        if not (
            values["JARVIS_GOOGLE_CLIENT_ID"].strip()
            or self._connections.value("JARVIS_GOOGLE_CLIENT_ID")
        ):
            self.note.setText("Paste the OAuth client ID first (see above).")
            return False
        self._connections.save("gmail", values)
        return True

    def save(self) -> None:
        values = self.values()
        if any(v.strip() for v in values.values()):
            self._connections.save("gmail", values)
        self.accept()

    def sign_in(self) -> None:
        gmail = self._connections.gmail
        if gmail is None or not self._save_client():
            return
        opener = self._connections.open_url or _open_in_browser
        self.note.setText("Finish signing in on Google's page in your browser…")
        self.sign_in_button.setEnabled(False)

        async def go() -> None:
            try:
                address = await gmail.sign_in(opener)
            except Exception as exc:
                self.note.setText(f"Gmail wasn't connected: {exc}.")
            else:
                self.note.setText(f"Signed in as {address} (read-only).")
                self.refresh()
            finally:
                self.sign_in_button.setEnabled(True)

        try:
            self._busy = asyncio.ensure_future(go())
        except RuntimeError:  # no running event loop (tests)
            self.sign_in_button.setEnabled(True)
            self.note.setText("Google can't be reached right now.")

    def remove_selected(self) -> None:
        gmail = self._connections.gmail
        item = self.accounts.currentItem()
        if gmail is None or item is None:
            return
        address = item.text()

        async def go() -> None:
            await gmail.remove(address)
            self.note.setText(f"Removed {address}; JARVIS's access was revoked at Google.")
            self.refresh()

        try:
            self._busy = asyncio.ensure_future(go())
        except RuntimeError:
            self.note.setText("Couldn't remove it right now.")


def _open_in_browser(url: str) -> None:
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    QDesktopServices.openUrl(QUrl(url))


class LoginsDialog(QDialog):
    """Saved logins for the "Fill in a login" node: add, edit and remove. Pick one in
    the list to change it; a blank password keeps the saved one. ``on_renamed`` is told
    when a login's name changes, so workflows using it can follow."""

    def __init__(
        self,
        parent: QWidget | None,
        connections: Connections,
        on_renamed: Callable[[str, str], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._logins = connections.logins
        self._on_renamed = on_renamed
        self._editing: str | None = None  # the name of the login being changed
        self.setWindowTitle("Saved logins")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        heading = QLabel("Saved logins")
        heading.setObjectName("title")
        layout.addWidget(heading)
        about = QLabel(
            "Kept in Windows Credential Manager, encrypted for your Windows account. A login "
            "is only typed into a window whose title contains its window words, and only by "
            "a workflow. No AI ever sees it. Pick one below to change it."
        )
        about.setWordWrap(True)
        about.setObjectName("hint")
        layout.addWidget(about)
        self.list = QListWidget()
        self.list.setFixedHeight(120)
        self.list.currentItemChanged.connect(lambda item, _old: self._pick(item))
        layout.addWidget(self.list)
        row = QHBoxLayout()
        self.new_button = QPushButton("+ New login")
        self.new_button.clicked.connect(self.start_new)
        row.addWidget(self.new_button)
        row.addStretch(1)
        remove = QPushButton("Remove selected")
        remove.setObjectName("danger")
        remove.clicked.connect(self.remove_selected)
        row.addWidget(remove)
        layout.addLayout(row)
        self.form_title = QLabel("NEW LOGIN")
        self.form_title.setObjectName("section")
        layout.addWidget(self.form_title)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. College portal")
        self.site = QLineEdit()
        self.site.setPlaceholderText("words in the window's title, e.g. Student Login")
        self.username = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        for text, box in (
            ("NAME", self.name),
            ("WINDOW TITLE CONTAINS", self.site),
            ("USERNAME", self.username),
            ("PASSWORD", self.password),
        ):
            label = QLabel(text)
            label.setObjectName("section")
            form.addRow(label, box)
        layout.addLayout(form)
        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setObjectName("hint")
        layout.addWidget(self.note)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        done = QPushButton("Done")
        done.clicked.connect(self.accept)
        self.save_button = QPushButton("Save login")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self.add)
        buttons.addWidget(done)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self, select: str | None = None) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for login in self._logins.all():
            item = QListWidgetItem(login.describe())
            item.setData(Qt.ItemDataRole.UserRole, login.name)
            self.list.addItem(item)
            if select is not None and login.name.lower() == select.lower():
                self.list.setCurrentItem(item)
        self.list.blockSignals(False)

    def _pick(self, item: QListWidgetItem | None) -> None:
        """Fill the form with the chosen login (never its password)."""
        if item is None:
            return
        name = str(item.data(Qt.ItemDataRole.UserRole))
        login = next((x for x in self._logins.all() if x.name == name), None)
        if login is None:
            return
        self._editing = login.name
        self.name.setText(login.name)
        self.site.setText(login.site)
        self.username.setText(login.username)
        self.password.clear()
        self.password.setPlaceholderText("Saved; leave blank to keep it")
        self.form_title.setText(f"CHANGE “{login.name.upper()}”")
        self.save_button.setText("Save changes")
        self.note.setText("")

    def start_new(self) -> None:
        self._editing = None
        self.list.blockSignals(True)
        self.list.clearSelection()
        self.list.setCurrentRow(-1)
        self.list.blockSignals(False)
        for box in (self.name, self.site, self.username, self.password):
            box.clear()
        self.password.setPlaceholderText("")
        self.form_title.setText("NEW LOGIN")
        self.save_button.setText("Save login")
        self.name.setFocus()

    def add(self) -> None:
        """Save the form: a new login, or changes to the one picked."""
        name, site = self.name.text().strip(), self.site.text().strip()
        username, password = self.username.text().strip(), self.password.text()
        old = self._editing
        if old is not None and not password:
            kept = self._logins.get(old)
            password = kept.password if kept is not None else ""
        if not (name and site and (username or password)):
            self.note.setText("Give it a name, the window title words and the login.")
            return
        renamed = old is not None and old.strip().lower() != name.lower()
        if (old is None or renamed) and any(
            x.name.lower() == name.lower() for x in self._logins.all()
        ):
            self.note.setText(f"There's already a login called “{name}”. Pick it to change it.")
            return
        try:
            self._logins.put(Login(name, site, username, password))
            if renamed and old is not None:
                self._logins.remove(old)
        except Exception as exc:
            self.note.setText(f"Couldn't save it ({type(exc).__name__}).")
            return
        if renamed and old is not None and self._on_renamed is not None:
            self._on_renamed(old, name)
        self.start_new()
        self.refresh()
        self.note.setText(f"Updated “{name}”." if old is not None else f"Saved “{name}”.")

    def remove_selected(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        self._logins.remove(str(item.data(Qt.ItemDataRole.UserRole)))
        self.start_new()
        self.refresh()
