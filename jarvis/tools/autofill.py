"""The workflow "Fill in a login" node: types a saved login into a sign-in window.

Owner, 2026-10-26: "a feature where in it will find the username box and type in the
username, then the password box to type in the password". The login (username,
password, and the words the window's title must contain) is saved once in Windows
Credential Manager from the node's settings (``jarvis/app/secrets.py``). The tool takes
only the login's name, so no AI ever sees a username or password, and it's in the
``WORKFLOW_ONLY`` group: never offered to a model and refused unless a workflow step
runs it. It asks first (MEDIUM), only types into a window whose title contains the
saved words, and never logs what it typed.
"""

from __future__ import annotations

from typing import Protocol

from jarvis.app.secrets import LoginStore
from jarvis.tools.registry import WORKFLOW_ONLY, Risk, ToolRegistry, ToolResult, ToolSpec
from jarvis.tools.ui import UiError


class LoginFiller(Protocol):
    async def fill_login(self, site: str, username: str, password: str, submit: bool) -> str: ...


def register_autofill_tools(
    registry: ToolRegistry, filler: LoginFiller | None, logins: LoginStore
) -> None:
    def describe(args: dict[str, object]) -> str:
        name = str(args.get("login", ""))
        saved = next((x for x in logins.all() if x.name.lower() == name.lower()), None)
        what = saved.describe() if saved is not None else f"“{name}”"
        return f"Fill in the login {what}" + (" and press Enter" if args.get("submit") else "")

    async def fill_login(login: str, submit: bool = False) -> ToolResult:
        if filler is None:
            return ToolResult(False, "Filling in logins only works on Windows.")
        saved = logins.get(login)
        if saved is None:
            return ToolResult(
                False,
                f"There's no saved login called “{login}”. Add it in the node's settings.",
            )
        try:
            filled = await filler.fill_login(saved.site, saved.username, saved.password, submit)
        except UiError as exc:
            return ToolResult(False, f"Couldn't fill in the login: {exc}.")
        done = f"Filled in the {filled} for {saved.name}"
        return ToolResult(True, done + (" and pressed Enter." if submit else "."))

    registry.register(
        ToolSpec(
            "fill_login",
            "Type a saved login into its sign-in window (workflows only).",
            {
                "type": "object",
                "properties": {
                    "login": {"type": "string", "maxLength": 80},
                    "submit": {"type": "boolean"},
                },
                "required": ["login"],
            },
            fill_login,
            Risk.MEDIUM,
            describe,
            group=WORKFLOW_ONLY,
        )
    )
