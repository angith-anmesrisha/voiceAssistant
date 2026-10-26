"""Saved logins for the workflow "Fill in a login" node.

Owner, 2026-10-26: "a feature where in it will find the username box and type in the
username, then the password box to type in the password". Logins are kept in Windows
Credential Manager (the same store Windows uses for its own saved passwords, encrypted
for your Windows account), one generic credential per login named "JARVIS login: <name>".
The site words (which window it may be typed into) go in the credential's comment.

Passwords are only ever read by the fill-in tool at the moment it types them. They are
never logged, never shown, never put in workflows.json, the history or the memory
folder, and never sent to an AI.
"""

from __future__ import annotations

import contextlib
import sys
from dataclasses import dataclass
from typing import Protocol

from loguru import logger

PREFIX = "JARVIS login: "


@dataclass(frozen=True)
class Login:
    name: str  # what workflows call it, e.g. "College portal"
    site: str  # words the window's title must contain, e.g. "portal.college.edu"
    username: str
    password: str = ""  # only filled in by ``LoginStore.get``

    def describe(self) -> str:
        return f"{self.name} ({self.username} on windows titled “{self.site}”)"


class LoginStore(Protocol):
    def all(self) -> list[Login]: ...  # without passwords
    def get(self, name: str) -> Login | None: ...  # with the password
    def put(self, login: Login) -> None: ...
    def remove(self, name: str) -> None: ...


class MemoryLogins:
    """Logins kept only while JARVIS runs (tests, and PCs without Credential Manager)."""

    def __init__(self) -> None:
        self._items: dict[str, Login] = {}

    def all(self) -> list[Login]:
        return [Login(x.name, x.site, x.username) for x in self._items.values()]

    def get(self, name: str) -> Login | None:
        return self._items.get(_key(name))

    def put(self, login: Login) -> None:
        self._items[_key(login.name)] = login

    def remove(self, name: str) -> None:
        self._items.pop(_key(name), None)


class WindowsLogins:
    """Logins in Windows Credential Manager (pywin32's win32cred)."""

    def all(self) -> list[Login]:
        import win32cred

        try:
            found = win32cred.CredEnumerate(PREFIX + "*", 0) or []
        except Exception:  # none saved yet raises ERROR_NOT_FOUND
            return []
        logins = []
        for cred in found:
            target = str(cred.get("TargetName") or "")
            if target.startswith(PREFIX):
                logins.append(
                    Login(
                        target.removeprefix(PREFIX),
                        str(cred.get("Comment") or ""),
                        str(cred.get("UserName") or ""),
                    )
                )
        return sorted(logins, key=lambda x: x.name.lower())

    def get(self, name: str) -> Login | None:
        import win32cred

        try:
            cred = win32cred.CredRead(PREFIX + name.strip(), win32cred.CRED_TYPE_GENERIC, 0)
        except Exception:
            return None
        blob = cred.get("CredentialBlob") or b""
        password = blob.decode("utf-16-le") if isinstance(blob, bytes) else str(blob)
        return Login(
            name.strip(),
            str(cred.get("Comment") or ""),
            str(cred.get("UserName") or ""),
            password,
        )

    def put(self, login: Login) -> None:
        import win32cred

        win32cred.CredWrite(
            {
                "Type": win32cred.CRED_TYPE_GENERIC,
                "TargetName": PREFIX + login.name.strip(),
                "UserName": login.username,
                "CredentialBlob": login.password,
                "Comment": login.site,
                "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
            },
            0,
        )
        logger.info("Saved a login to Credential Manager")  # never its name or values

    def remove(self, name: str) -> None:
        import win32cred

        with contextlib.suppress(Exception):  # already gone
            win32cred.CredDelete(PREFIX + name.strip(), win32cred.CRED_TYPE_GENERIC, 0)


def _key(name: str) -> str:
    return " ".join(name.lower().split())


def login_store() -> LoginStore:
    return WindowsLogins() if sys.platform == "win32" else MemoryLogins()
