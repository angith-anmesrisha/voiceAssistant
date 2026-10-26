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
            Field("JARVIS_EMAIL_ADDRESS", "Email address", hint="you@gmail.com"),
            Field(
                "JARVIS_EMAIL_PASSWORD",
                "App password",
                secret=True,
                hint="16 letters from Google Account > Security > App passwords",
            ),
            Field(
                "JARVIS_EMAIL_IMAP",
                "IMAP server (optional)",
                required=False,
                hint="filled in for Gmail, Outlook, Yahoo, iCloud and Zoho",
            ),
        ),
        "Use an app password, not your normal one (Google asks for 2-Step Verification "
        "first). JARVIS only reads mail and saves replies to Drafts; it never sends. Any "
        "IMAP mailbox works.",
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
    ) -> None:
        self.logins = logins if logins is not None else MemoryLogins()
        self._get, self._save = get, save
        self.find_chat = find_chat

    def value(self, key: str) -> str:
        return (self._get(key) or "").strip()

    def connected(self, service: str) -> bool:
        if service == "logins":
            return bool(self.login_names())
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
            return f"Connected as {self.value('JARVIS_EMAIL_ADDRESS')}"
        return "Connected"

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


class LoginsDialog(QDialog):
    """Saved logins for the "Fill in a login" node: add and remove."""

    def __init__(self, parent: QWidget | None, connections: Connections) -> None:
        super().__init__(parent)
        self._logins = connections.logins
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
            "is only typed into a window whose title contains its window words, after you "
            "say yes. No AI ever sees it."
        )
        about.setWordWrap(True)
        about.setObjectName("hint")
        layout.addWidget(about)
        self.list = QListWidget()
        self.list.setFixedHeight(120)
        layout.addWidget(self.list)
        remove = QPushButton("Remove selected")
        remove.setObjectName("danger")
        remove.clicked.connect(self.remove_selected)
        layout.addWidget(remove, 0, Qt.AlignmentFlag.AlignRight)
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
        add = QPushButton("Save login")
        add.setObjectName("primary")
        add.clicked.connect(self.add)
        buttons.addWidget(done)
        buttons.addWidget(add)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self) -> None:
        self.list.clear()
        for login in self._logins.all():
            item = QListWidgetItem(login.describe())
            item.setData(Qt.ItemDataRole.UserRole, login.name)
            self.list.addItem(item)

    def add(self) -> None:
        name, site = self.name.text().strip(), self.site.text().strip()
        username, password = self.username.text().strip(), self.password.text()
        if not (name and site and (username or password)):
            self.note.setText("Give it a name, the window title words and the login.")
            return
        try:
            self._logins.put(Login(name, site, username, password))
        except Exception as exc:
            self.note.setText(f"Couldn't save it ({type(exc).__name__}).")
            return
        for box in (self.name, self.site, self.username, self.password):
            box.clear()
        self.note.setText(f"Saved “{name}”.")
        self.refresh()

    def remove_selected(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        self._logins.remove(str(item.data(Qt.ItemDataRole.UserRole)))
        self.refresh()
