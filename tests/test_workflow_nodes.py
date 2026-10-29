"""Round 16 nodes: key sequences, typing, JSON actions, logins, AI clicks and Telegram."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from jarvis.agent.toolbudget import groups_for
from jarvis.agent.workflows import (
    ACTIONS,
    Step,
    StepError,
    Trigger,
    Workflow,
    json_calls,
    key_calls,
    parse_proposal,
    proposal_prompt,
    step_calls,
    workflow_request,
)
from jarvis.app.events import StepStatus
from jarvis.app.secrets import Login, MemoryLogins
from jarvis.tools.autofill import register_autofill_tools
from jarvis.tools.registry import WORKFLOW_ONLY, Risk
from jarvis.tools.telegram import find_chat, register_telegram_tools
from jarvis.tools.ui import UiError, login_fields
from tests.test_assistant import ScriptedRouter, text_reply, tool_reply
from tests.test_workflow_builder import add_from_panel
from tests.test_workflows import WorkflowRig


def test_key_lines_become_presses_typing_and_pauses() -> None:
    calls = key_calls(["ctrl+a", "Type: Hello there", "tab x3", "wait: 500ms", "copy", "  "])
    assert calls == [
        ("press_keys", {"keys": "ctrl+a"}),
        ("type_text", {"text": "Hello there"}),
        ("press_keys", {"keys": "tab"}),
        ("press_keys", {"keys": "tab"}),
        ("press_keys", {"keys": "tab"}),
        ("wait", {"seconds": 0.5}),
        ("press_shortcut", {"shortcut": "copy"}),
    ]
    assert key_calls(["ctrl + shift + t"]) == [("press_keys", {"keys": "ctrl+shift+t"})]
    assert key_calls(["wait 2"]) == [("wait", {"seconds": 2.0})]


def test_an_app_is_raised_before_the_keys() -> None:
    step = Step("keys", {"app": "Notepad", "items": "ctrl+n\ntype: hi"})
    assert step_calls(step)[0] == ("open_app", {"name": "Notepad"})
    typed = Step("type_text", {"text": "Dear Sir", "enter": True})
    assert step_calls(typed) == [
        ("type_text", {"text": "Dear Sir"}),
        ("press_keys", {"keys": "enter"}),
    ]
    assert ACTIONS["keys"].describe(step.settings) == "Press ctrl+n, type: hi in Notepad"


def test_json_actions_are_parsed() -> None:
    text = json.dumps(
        [
            {"keys": "ctrl+n"},
            {"type": "Hello"},
            {"wait": 1},
            {"open": "Spotify"},
            {"tool": "set_volume", "args": {"percent": 30}},
            {"action": "lock_pc"},
            {"action": "keys", "settings": {"items": ["enter"]}},
        ]
    )
    assert json_calls(text) == [
        ("press_keys", {"keys": "ctrl+n"}),
        ("type_text", {"text": "Hello"}),
        ("wait", {"seconds": 1.0}),
        ("open_app", {"name": "Spotify"}),
        ("set_volume", {"percent": 30}),
        ("lock_pc", {}),
        ("press_keys", {"keys": "enter"}),
    ]
    assert json_calls('{"steps": [{"keys": "f5"}]}') == [("press_keys", {"keys": "f5"})]
    for bad, says in [
        ("[{", "isn't valid"),
        ('[{"run": "del *"}]', "needs keys"),
        ('[{"action": "summarise"}]', "can't run inside JSON"),
        ('[{"action": "nope"}]', "no action called"),
        ('"text"', "list of actions"),
    ]:
        with pytest.raises(StepError, match=says):
            json_calls(bad)
    assert ACTIONS["json_actions"].describe({"json": "[{"}) == "Run JSON actions (check the JSON)"


async def test_a_keyboard_step_presses_each_key_in_order(tmp_path: Path) -> None:
    rig = WorkflowRig(tmp_path, ScriptedRouter())
    rig.workflows.put(
        Workflow(
            "Copy all",
            Trigger("phrase", ["copy all"]),
            [Step("keys", {"items": "ctrl+a\nctrl+c\ntype: done"})],
        )
    )
    reply = await rig.assistant.handle("copy all")
    assert reply.text.startswith("Copy all is done.")
    pressed = [c for c in rig.desktop.calls if c[0] in ("press", "type")]
    assert pressed == [("press", ("ctrl", "a")), ("press", ("ctrl", "c")), ("type", "done")]
    assert rig.confirms == []  # ordinary keys never ask


async def test_risky_keys_ask_once_when_workflows_ask_first(tmp_path: Path) -> None:
    rig = WorkflowRig(tmp_path, ScriptedRouter())
    rig.assistant._workflow_asks = lambda: True
    rig.workflows.put(
        Workflow(
            "Send it",
            Trigger("phrase", ["send it"]),
            [Step("keys", {"items": "type: hello\nenter\nwait: 0\nenter"})],
        )
    )
    await rig.assistant.handle("send it")
    assert len(rig.confirms) == 1
    assert "Press enter" in rig.confirms[0].description
    assert [c for c in rig.desktop.calls if c[0] == "press"] == [("press", ("enter",))] * 2

    (tmp_path / "no").mkdir()
    no = WorkflowRig(tmp_path / "no", ScriptedRouter(), approve=False)
    no.assistant._workflow_asks = lambda: True
    no.workflows.put(rig.workflows.all[0])
    await no.assistant.handle("send it")
    assert [c for c in no.desktop.calls if c[0] in ("press", "type")] == []
    assert no.plans[-1].steps[0].status is StepStatus.FAILED


async def test_workflows_run_without_asking_by_default(tmp_path: Path) -> None:
    rig = WorkflowRig(tmp_path, ScriptedRouter())
    filler, _ = _with_logins(rig)
    rig.workflows.put(
        Workflow(
            "Go",
            Trigger("phrase", ["go"]),
            [
                Step("keys", {"items": "type: hello\nenter"}),
                Step("login", {"login": "College portal"}),
                Step("tool", {"name": "lock_pc"}),
                Step(
                    "tool", {"name": "shutdown_or_restart_pc", "arguments": '{"action": "restart"}'}
                ),
            ],
        )
    )
    await rig.assistant.handle("go")
    assert [c for c in rig.desktop.calls if c[0] in ("press", "lock")] == [
        ("press", ("enter",)),
        ("lock", None),
    ]
    assert filler.filled == [("Student Login", "s1234", "hunter2", True)]
    assert [c.tool for c in rig.confirms] == ["shutdown_or_restart_pc"]  # HIGH still asks


async def test_a_failing_action_stops_the_json_step(tmp_path: Path) -> None:
    rig = WorkflowRig(tmp_path, ScriptedRouter())
    rig.workflows.put(
        Workflow(
            "Json",
            Trigger("phrase", ["json"]),
            [Step("json_actions", {"json": '[{"tool": "no_such_tool"}, {"keys": "f5"}]'})],
        )
    )
    await rig.assistant.handle("json")
    step = rig.plans[-1].steps[0]
    assert step.status is StepStatus.FAILED and "no_such_tool" in step.note
    assert [c for c in rig.desktop.calls if c[0] == "press"] == []


# --- logins ---


@pytest.mark.parametrize(
    ("boxes", "expected"),
    [
        ([("Address and search bar", False), ("Email", False), ("Password", True)], (1, 2)),
        ([("Address and search bar", False), ("", False), ("", True)], (1, 2)),
        ([("Search", False), ("", True)], (None, 1)),  # step two of a two-step sign-in
        ([("Address bar", False), ("Username or email", False)], (1, None)),
        ([("Search", False), ("Comment", False), ("Name", False)], (None, None)),
        ([("Roll number", False), ("Remarks", False), ("Password", True)], (0, 2)),
    ],
)
def test_the_username_and_password_boxes_are_found(
    boxes: list[tuple[str, bool]], expected: tuple[int | None, int | None]
) -> None:
    assert login_fields(boxes) == expected


class FakeFiller:
    def __init__(self) -> None:
        self.filled: list[tuple[str, str, str, bool]] = []

    async def fill_login(self, site: str, username: str, password: str, submit: bool) -> str:
        if site == "missing":
            raise UiError("no open window matches “missing”")
        self.filled.append((site, username, password, submit))
        return "username and password"


def _with_logins(rig: WorkflowRig) -> tuple[FakeFiller, MemoryLogins]:
    filler, logins = FakeFiller(), MemoryLogins()
    logins.put(Login("College portal", "Student Login", "s1234", "hunter2"))
    register_autofill_tools(rig.assistant._registry, filler, logins)
    return filler, logins


async def test_a_login_step_fills_in_the_saved_login_after_a_yes(tmp_path: Path) -> None:
    rig = WorkflowRig(tmp_path, ScriptedRouter())
    rig.assistant._workflow_asks = lambda: True
    filler, logins = _with_logins(rig)
    assert logins.all()[0].password == ""  # listing never carries passwords
    rig.workflows.put(
        Workflow(
            "Portal",
            Trigger("phrase", ["portal"]),
            [Step("login", {"login": "college portal", "submit": True})],
        )
    )
    reply = await rig.assistant.handle("portal")
    assert filler.filled == [("Student Login", "s1234", "hunter2", True)]
    assert [c.tool for c in rig.confirms] == ["fill_login"]
    assert "hunter2" not in rig.confirms[0].description
    assert "s1234 on windows titled “Student Login”" in rig.confirms[0].description
    everything = reply.text + " ".join(e.text for e in rig.entries)
    assert "hunter2" not in everything
    assert "Filled in the username and password for College portal" in everything


async def test_no_model_can_fill_in_a_login(tmp_path: Path) -> None:
    router = ScriptedRouter(tool_reply("fill_login", login="College portal"), text_reply("Ok."))
    rig = WorkflowRig(tmp_path, router)
    filler, _ = _with_logins(rig)
    spec = rig.assistant._registry.get("fill_login")
    assert spec is not None and spec.group == WORKFLOW_ONLY
    await rig.assistant.handle("log me into the college portal please")
    names = [t["function"]["name"] for t in router.requests[0]["tools"]]
    assert "fill_login" not in names  # never offered ...
    assert filler.filled == [] and rig.confirms == []  # ... and refused if named anyway
    sent = router.requests[1]["messages"][-1]["content"]
    assert "only runs from a workflow step" in sent


async def test_a_missing_login_or_window_fails_plainly(tmp_path: Path) -> None:
    rig = WorkflowRig(tmp_path, ScriptedRouter())
    _filler, logins = _with_logins(rig)
    logins.put(Login("Gone", "missing", "u", "p"))
    registry: Any = rig.assistant._registry
    spec = registry.get("fill_login")
    nothing = await registry.run(spec, {"login": "Bank"})
    assert not nothing.ok and "no saved login called “Bank”" in nothing.summary
    closed = await registry.run(spec, {"login": "Gone"})
    assert not closed.ok and "no open window" in closed.summary


# --- AI clicks and Prompt JARVIS ---


async def test_an_ai_click_asks_jarvis_to_read_the_window_and_click(tmp_path: Path) -> None:
    router = ScriptedRouter(text_reply("I clicked Sign in."))
    rig = WorkflowRig(tmp_path, router)
    rig.workflows.put(
        Workflow(
            "Sign in",
            Trigger("phrase", ["sign in"]),
            [Step("ai_click", {"target": "the blue Sign in button", "app": "Chrome"})],
        )
    )
    await rig.assistant.handle("sign in")
    asked = router.requests[0]["messages"][-1]["content"]
    assert "in the Chrome window: the blue Sign in button" in asked and "read_ui" in asked
    assert "ui" in groups_for(asked)  # so the request is offered the window tools
    assert ACTIONS["ask_jarvis"].title == "Prompt JARVIS"


# --- Telegram ---


def _telegram(handler: Any, keys: dict[str, str]) -> Any:
    from jarvis.tools.registry import ToolRegistry

    registry = ToolRegistry()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    register_telegram_tools(registry, client, key=keys.get)
    return registry, registry.get("send_telegram"), client


async def test_telegram_sends_through_the_bot_api() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    keys = {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "42"}
    registry, spec, client = _telegram(handler, keys)
    assert spec.risk_for({}) is Risk.MEDIUM and spec.group == "ui"
    result = await registry.run(spec, {"message": "Build finished"})
    assert result.ok
    assert str(seen[0].url) == "https://api.telegram.org/bot123:abc/sendMessage"
    assert json.loads(seen[0].content) == {"chat_id": "42", "text": "Build finished"}
    await registry.run(spec, {"message": "x", "chat": "@mychannel"})
    assert json.loads(seen[1].content)["chat_id"] == "@mychannel"
    await client.aclose()


async def test_telegram_errors_never_show_the_token() -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"can't reach {request.url}")

    keys = {"TELEGRAM_BOT_TOKEN": "123:secret", "TELEGRAM_CHAT_ID": "42"}
    for handler, says in [(refused, "token isn't valid"), (broken, "ConnectError")]:
        registry, spec, client = _telegram(handler, keys)
        result = await registry.run(spec, {"message": "hi"})
        assert not result.ok and says in result.summary and "secret" not in result.summary
        await client.aclose()
    registry, spec, client = _telegram(refused, {})
    assert "isn't set up" in (await registry.run(spec, {"message": "hi"})).summary
    await client.aclose()


async def test_the_chat_is_found_from_the_newest_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/getUpdates")
        updates = [
            {"message": {"chat": {"id": 1, "first_name": "Old"}}},
            {"message": {"chat": {"id": 99, "first_name": "Angith"}}},
        ]
        return httpx.Response(200, json={"ok": True, "result": updates})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await find_chat(client, "t") == ("99", "Angith")


# --- JARVIS designing whole workflows ---


@pytest.mark.parametrize(
    "text",
    [
        "Make me a workflow that logs into my college portal every morning",
        "can you build a login workflow for my portal",
        "Jarvis, design a morning workflow for me",
        "I want a workflow that sends my inbox summary to Telegram",
        "automate my mornings: weather, then my tasks",
    ],
)
def test_more_ways_to_ask_for_a_workflow(text: str) -> None:
    assert workflow_request(text)


@pytest.mark.parametrize("text", ["automate this", "how do workflows work", "make me a coffee"])
def test_still_not_workflow_requests(text: str) -> None:
    assert not workflow_request(text)


def test_a_designed_workflow_keeps_its_branches_and_never_a_password() -> None:
    reply = json.dumps(
        {
            "name": "Portal check",
            "trigger": {"kind": "time", "at": "08:00"},
            "steps": [
                {"action": "open_website", "settings": {"url": "https://portal.example.edu"}},
                {"action": "wait", "settings": {"seconds": 4}},
                {"action": "login", "settings": {"login": "College portal", "password": "x"}},
                {"action": "run_shell", "settings": {}},
                {"action": "condition", "settings": {"test": "has errors"}},
                {"action": "telegram", "settings": {"message": "Portal login failed"}},
                {"action": "say", "settings": {"text": "Logged in."}},
            ],
            "links": [
                ["trigger", "out", 1],
                [1, "out", 2],
                [2, "out", 3],
                [3, "out", 5],
                [5, "true", 6],
                [5, "false", 7],
                [7, "out", 1],  # a loop: dropped
                [3, "out", 4],  # to a dropped step
                [6, "maybe", 7],  # no such output
            ],
        }
    )
    workflow = parse_proposal(reply)
    actions = [s.action for s in workflow.steps]
    assert actions == ["open_website", "wait", "login", "condition", "telegram", "say"]
    assert "password" not in workflow.steps[2].settings
    ids = {s.action: s.id for s in workflow.steps}
    assert workflow.links is not None
    pairs = {(link.src, link.port, link.dst) for link in workflow.links}
    assert (ids["condition"], "true", ids["telegram"]) in pairs
    assert (ids["condition"], "false", ids["say"]) in pairs
    assert len(pairs) == 6
    assert "login" in proposal_prompt() and "never put a username or password" in proposal_prompt()


def test_a_design_without_links_runs_in_order() -> None:
    reply = '{"name": "x", "steps": [{"action": "say", "settings": {"text": "a"}}], "links": []}'
    assert parse_proposal(reply).links is None


# --- the builder: setup cards, saved logins, search ---


class SavedKeys:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def save(self, name: str, value: str) -> bool:
        self.values[name] = value
        return True


@pytest.fixture(scope="module")
def qapp() -> Any:
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _builder(tmp_path: Path, keys: SavedKeys, logins: MemoryLogins) -> Any:
    from jarvis.agent.workflows import WorkflowStore
    from jarvis.ui.connections import Connections
    from jarvis.ui.workflows import WorkflowBuilder

    store = WorkflowStore(tmp_path / "workflows.json")
    store.put(Workflow("Study Mode", Trigger("phrase", ["study"]), [Step("say", {"text": "a"})]))
    store.put(Workflow("Inbox", Trigger("manual"), [Step("email_summary", {})]))
    connections = Connections(logins, get=keys.get, save=keys.save)
    return WorkflowBuilder(store, lambda _w: None, connections)


def test_a_gmail_node_offers_to_connect_once(qapp: Any, tmp_path: Path) -> None:
    from jarvis.app.secrets import MemoryTokens
    from jarvis.tools.gmail_api import GmailAccounts
    from jarvis.ui.connections import GmailDialog

    keys = SavedKeys()
    builder = _builder(tmp_path, keys, MemoryLogins())
    tokens = MemoryTokens()
    builder.connections.gmail = GmailAccounts(httpx.AsyncClient(), tokens, key=keys.get)
    builder.search.setText("inbox")
    assert builder.visible_workflows() == ["Inbox"]
    builder.list.setCurrentRow(1)
    step = builder.current.steps[0]
    builder.select_node(step.id)
    assert builder.inspector.fields["connection"].text() == "Connect"
    dialog = GmailDialog(builder, builder.connections)
    assert "JARVIS_EMAIL_PASSWORD" not in dialog.inputs  # never asks for a password
    dialog.sign_in()
    assert "client ID first" in dialog.note.text()
    dialog.inputs["JARVIS_GOOGLE_CLIENT_ID"].setText("123.apps.googleusercontent.com")
    dialog.inputs["JARVIS_GOOGLE_CLIENT_SECRET"].setText("shh")
    dialog.save()
    assert keys.values["JARVIS_GOOGLE_CLIENT_SECRET"] == "shh"
    again = GmailDialog(builder, builder.connections)
    assert again.inputs["JARVIS_GOOGLE_CLIENT_SECRET"].text() == ""  # a secret is never shown
    tokens.put("me@gmail.com", "refresh-1")
    tokens.put("work@gmail.com", "refresh-2")
    again.refresh()
    assert [again.accounts.item(i).text() for i in range(again.accounts.count())] == [
        "me@gmail.com",
        "work@gmail.com",
    ]
    status = builder.connections.status("gmail")
    assert status == "Signed in (read-only): me@gmail.com, work@gmail.com"
    builder.select_node(step.id)
    assert builder.inspector.fields["connection"].text() == "Change"


def test_logins_are_saved_and_picked_in_the_login_node(qapp: Any, tmp_path: Path) -> None:
    from PySide6.QtWidgets import QComboBox

    from jarvis.ui.connections import LoginsDialog

    logins = MemoryLogins()
    builder = _builder(tmp_path, SavedKeys(), logins)
    dialog = LoginsDialog(builder, builder.connections)
    dialog.name.setText("College portal")
    dialog.site.setText("Student Login")
    dialog.username.setText("s1234")
    dialog.password.setText("hunter2")
    dialog.add()
    assert dialog.password.text() == "" and dialog.list.count() == 1
    assert "hunter2" not in dialog.list.item(0).text()
    assert logins.get("college portal") is not None
    builder.list.setCurrentRow(0)
    step = add_from_panel(builder, "login")
    builder.select_node(step.id)
    field = builder.inspector.fields["login"]
    assert isinstance(field, QComboBox) and field.itemText(0) == "College portal"
    field.setCurrentText("College portal")
    assert builder.current.step(step.id).settings["login"] == "College portal"
    assert "hunter2" not in json.dumps(builder.current.to_json())
    dialog.list.setCurrentRow(0)
    dialog.remove_selected()
    assert logins.all() == []


def test_the_new_nodes_are_in_the_panel_and_the_tools_are_one_pill(
    qapp: Any, tmp_path: Path
) -> None:
    builder = _builder(tmp_path, SavedKeys(), MemoryLogins())
    builder.open_palette()
    builder.pane.node_panel.search.setText("keyboard")
    names = builder.pane.node_panel.visible_names()
    assert "keys" in names
    for button in builder.pane.tools:
        assert button.parent() is builder.pane.pill
    from PySide6.QtWidgets import QLabel

    builder.inspector.show_workflow(builder.current)
    chips = [w.text() for w in builder.inspector.findChildren(QLabel) if w.objectName() == "chip"]
    assert chips[:2] == ["Tab", "Del"]


def test_the_builder_panels_can_be_resized(qapp: Any, tmp_path: Path) -> None:
    builder = _builder(tmp_path, SavedKeys(), MemoryLogins())
    builder.resize(1400, 820)
    builder.show()
    qapp.processEvents()
    assert builder.splitter.count() == 3
    builder.splitter.setSizes([320, 600, 480])
    qapp.processEvents()
    sidebar, _canvas, inspector = builder.splitter.sizes()
    assert sidebar > 280 and inspector > 420  # no longer fixed widths
    builder.toggle_logs()
    qapp.processEvents()
    assert builder.canvas_split.sizes()[1] >= 150
    builder.close()


def test_save_is_never_greyed_out_and_points_to_what_is_missing(qapp: Any, tmp_path: Path) -> None:
    from jarvis.agent.workflows import WorkflowStore
    from jarvis.ui.workflows import WorkflowBuilder

    store = WorkflowStore(tmp_path / "workflows.json")
    builder = WorkflowBuilder(store, lambda _w: None)
    builder.new_workflow()
    builder.name.setText("Valorant")
    assert builder.save_button.isEnabled()
    assert not builder.save()  # no steps yet: the node panel opens
    assert "Add at least one step" in builder.state.text()
    assert not builder.pane.node_panel.isHidden()
    step = add_from_panel(builder, "open_app")
    assert not builder.save()
    assert builder.inspector.showing == step.id  # its missing setting is shown
    builder.set_setting(step.id, "app", "Riot Client")
    assert builder.save()
    saved = store.all[0]
    assert saved.name == "Valorant" and saved.trigger.phrases == ["valorant"]
    assert "SAY “VALORANT”" in builder.state.text()


def test_a_saved_login_can_be_changed_without_retyping_it(qapp: Any, tmp_path: Path) -> None:
    from jarvis.ui.connections import LoginsDialog

    logins = MemoryLogins()
    logins.put(Login("Valorant", "Riot Client", "angith", "old-pass"))
    builder = _builder(tmp_path, SavedKeys(), logins)
    builder._store.put(
        Workflow("Play", Trigger("phrase", ["play"]), [Step("login", {"login": "valorant"})])
    )
    dialog = LoginsDialog(builder, builder.connections, builder.rename_login)
    dialog.list.setCurrentRow(0)
    assert dialog.name.text() == "Valorant" and dialog.username.text() == "angith"
    assert dialog.password.text() == ""  # never shown
    dialog.username.setText("angith2")
    dialog.add()  # blank password: kept
    saved = logins.get("Valorant")
    assert saved is not None and (saved.username, saved.password) == ("angith2", "old-pass")
    assert len(logins.all()) == 1 and "Updated" in dialog.note.text()

    dialog.list.setCurrentRow(0)
    dialog.name.setText("Riot")
    dialog.password.setText("new-pass")
    dialog.add()
    assert [x.name for x in logins.all()] == ["Riot"]
    riot = logins.get("Riot")
    assert riot is not None and riot.password == "new-pass"
    play = next(w for w in builder._store.all if w.name == "Play")
    assert play.steps[0].settings["login"] == "Riot"  # the workflow follows the rename

    dialog.start_new()
    dialog.name.setText("riot")
    dialog.site.setText("x")
    dialog.username.setText("y")
    dialog.add()
    assert "already a login" in dialog.note.text() and len(logins.all()) == 1


# --- Gmail nodes check the mailbox first ---


def _with_mailbox(rig: WorkflowRig, imap: Any) -> None:
    from jarvis.tools.email_imap import Mailbox, register_email_tools
    from jarvis.tools.registry import ToolRegistry

    box = None if imap is None else Mailbox("me@gmail.com", "pw", connect=lambda host: imap)
    mail = ToolRegistry()
    register_email_tools(mail, lambda: box)
    registry = rig.assistant._registry
    for name in mail.names():
        registry._tools[name] = mail._tools[name]


async def test_a_gmail_node_with_no_mailbox_fails_with_the_reason(tmp_path: Path) -> None:
    router = ScriptedRouter()  # no model is asked
    rig = WorkflowRig(tmp_path, router)
    _with_mailbox(rig, None)
    rig.workflows.put(Workflow("Inbox", Trigger("phrase", ["inbox"]), [Step("email_summary", {})]))
    await rig.assistant.handle("inbox")
    step = rig.plans[-1].steps[0]
    assert step.status is StepStatus.FAILED
    assert "no Gmail account is connected" in step.note and "Connect card" in step.note
    assert "restart" not in step.note
    assert router.requests == []


async def test_a_refused_sign_in_fails_the_gmail_node(tmp_path: Path) -> None:
    from tests.test_email import FakeImap

    rig = WorkflowRig(tmp_path, ScriptedRouter())
    _with_mailbox(rig, FakeImap({}, set(), refuse=True))
    rig.workflows.put(Workflow("Inbox", Trigger("phrase", ["inbox"]), [Step("email_tasks", {})]))
    await rig.assistant.handle("inbox")
    step = rig.plans[-1].steps[0]
    assert step.status is StepStatus.FAILED and "app password" in step.note


async def test_no_unread_mail_needs_no_model(tmp_path: Path) -> None:
    from tests.test_email import FakeImap

    router = ScriptedRouter()
    rig = WorkflowRig(tmp_path, router)
    _with_mailbox(rig, FakeImap({}, set()))
    rig.workflows.put(Workflow("Inbox", Trigger("phrase", ["inbox"]), [Step("email_summary", {})]))
    await rig.assistant.handle("inbox")
    step = rig.plans[-1].steps[0]
    assert step.status is StepStatus.DONE and step.note == "No unread email"
    assert router.requests == []


async def test_an_apology_with_a_curly_apostrophe_fails_the_step(tmp_path: Path) -> None:
    from tests.test_email import FakeImap, message

    router = ScriptedRouter(
        text_reply("I\u2019m unable to reach your Gmail account at the moment.")
    )
    rig = WorkflowRig(tmp_path, router)
    _with_mailbox(rig, FakeImap({"7": message(7, "Priya <p@w.com>", "Hi", "Hello")}, {"7"}))
    rig.workflows.put(Workflow("Inbox", Trigger("phrase", ["inbox"]), [Step("email_summary", {})]))
    await rig.assistant.handle("inbox")
    assert rig.plans[-1].steps[0].status is StepStatus.FAILED
    assert len(router.requests) == 1


def test_curly_apostrophes_count_as_cannot() -> None:
    from jarvis.agent.assistant import says_it_cannot

    assert says_it_cannot("I\u2019m unable to reach it")
    assert says_it_cannot("Sorry, I can\u2019t do that")
    assert says_it_cannot("I couldn\u2019t open it")
