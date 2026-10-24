from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jarvis.agent.assistant import Assistant, ConfirmRequest
from jarvis.app.config import ToolSettings
from jarvis.app.events import ChatEntry, EntryKind, EventBus
from jarvis.app.state import AppState, StateChanged, StateMachine
from jarvis.providers.base import Capability, ChatResult, ToolCall
from jarvis.providers.router import AllProvidersFailedError, Attempt
from jarvis.storage.db import HistoryStore
from jarvis.tools.apps import AppIndex
from jarvis.tools.builtin import build_registry
from jarvis.tools.registry import Risk
from tests.fakes import FakeDesktop


class ScriptedRouter:
    """Returns queued ChatResults and records what it was sent."""

    def __init__(self, *results: ChatResult | Exception) -> None:
        self.results = list(results)
        self.requests: list[dict[str, Any]] = []

    async def chat(
        self, messages: list[Any], *, tools: Any = None, vision: bool = False
    ) -> ChatResult:
        self.requests.append({"messages": list(messages), "tools": tools, "vision": vision})
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def tool_reply(tool: str, /, **args: Any) -> ChatResult:
    name = tool
    call = ToolCall("call_1", name, args)
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": name, "arguments": "{}"}}
        ],
    }
    return ChatResult("Groq", "gpt-oss", "", [call], message)


def text_reply(text: str, provider: str = "Groq") -> ChatResult:
    return ChatResult(provider, "model-x", text, [], {"role": "assistant", "content": text})


class Rig:
    def __init__(self, tmp_path: Path, router: ScriptedRouter, approve: bool = True) -> None:
        self.bus = EventBus()
        self.state = StateMachine(self.bus)
        self.store = HistoryStore(tmp_path / "h.sqlite3")
        self.desktop = FakeDesktop()
        self.router = router
        self.entries: list[ChatEntry] = []
        self.states: list[AppState] = []
        self.confirms: list[ConfirmRequest] = []
        self.approve = approve
        self.screen: bytes | None = b"\xff\xd8jpeg"

        async def on_entry(e: ChatEntry) -> None:
            self.entries.append(e)

        async def on_state(e: StateChanged) -> None:
            self.states.append(e.current)

        self.bus.subscribe(ChatEntry, on_entry)
        self.bus.subscribe(StateChanged, on_state)

        async def confirm(request: ConfirmRequest) -> bool:
            self.confirms.append(request)
            return self.approve

        async def capture() -> bytes | None:
            return self.screen

        self.assistant = Assistant(
            router,  # type: ignore[arg-type]
            self.store,
            build_registry(ToolSettings(), self.desktop, AppIndex([], apps_folder=False)),
            self.bus,
            self.state,
            confirm,
            capture,
        )


async def test_plain_question_is_answered_and_saved(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ScriptedRouter(text_reply("Paris.")))
    reply = await rig.assistant.handle("Capital of France?")
    assert (reply.text, reply.provider, reply.model) == ("Paris.", "Groq", "model-x")
    names = [t["function"]["name"] for t in rig.router.requests[0]["tools"]]
    assert "open_app" in names and "look_at_screen" in names  # the model decides
    assert rig.router.requests[0]["vision"] is False
    stored = await rig.store.messages(await rig.assistant.conversation_id())
    assert [(m.role, m.content, m.provider) for m in stored] == [
        ("user", "Capital of France?", None),
        ("assistant", "Paris.", "Groq"),
    ]
    assert rig.states == [AppState.THINKING, AppState.IDLE]
    assert [e.kind for e in rig.entries] == [EntryKind.USER, EntryKind.ASSISTANT]


async def test_history_is_sent_as_context(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ScriptedRouter(text_reply("One."), text_reply("Two.")))
    await rig.assistant.handle("first")
    await rig.assistant.handle("second")
    sent = rig.router.requests[1]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert sent[-1]["content"] == "second"


async def test_low_risk_tool_runs_without_confirmation(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path, ScriptedRouter(tool_reply("open_app", name="notepad"), text_reply("Opened."))
    )
    reply = await rig.assistant.handle("open notepad")
    assert reply.text == "Opened."
    assert rig.confirms == []
    assert rig.desktop.calls == [("launch", "notepad.exe")]
    assert AppState.ACTING in rig.states
    tool_message = rig.router.requests[1]["messages"][-1]
    assert tool_message["role"] == "tool" and '"ok": true' in tool_message["content"]


async def test_medium_risk_tool_declined_is_not_run(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        ScriptedRouter(tool_reply("type_text", text="hello"), text_reply("Okay, I won't.")),
        approve=False,
    )
    await rig.assistant.handle("type hello")
    assert rig.confirms[0].risk is Risk.MEDIUM
    assert ("type", "hello") not in rig.desktop.calls
    assert "declined" in rig.router.requests[1]["messages"][-1]["content"]
    assert any(e.kind is EntryKind.ACTION and e.text.startswith("Declined") for e in rig.entries)


async def test_high_risk_tool_asks_with_high_risk(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        ScriptedRouter(tool_reply("shutdown_or_restart_pc", action="shutdown"), text_reply("Ok.")),
        approve=True,
    )
    await rig.assistant.handle("shut down")
    assert rig.confirms[0].risk is Risk.HIGH
    assert rig.desktop.calls == [("power", ("shutdown", 60))]


async def test_unknown_tool_and_bad_args_are_reported_to_model(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        ScriptedRouter(
            tool_reply("run_shell", command="del C:\\"),
            tool_reply("press_shortcut", shortcut="alt_f4"),
            text_reply("I can't do that."),
        ),
    )
    reply = await rig.assistant.handle("wipe my disk")
    assert reply.text == "I can't do that."
    assert rig.desktop.calls == []
    contents = [r["messages"][-1]["content"] for r in rig.router.requests[1:]]
    assert "no tool called run_shell" in contents[0]
    assert "Invalid arguments" in contents[1]


async def test_tool_rounds_are_bounded(tmp_path: Path) -> None:
    loops = [tool_reply("get_datetime") for _ in range(15)]  # max_tool_rounds
    rig = Rig(tmp_path, ScriptedRouter(*loops, text_reply("Stopped.")))
    reply = await rig.assistant.handle("loop forever")
    assert reply.text == "Stopped."
    assert rig.router.requests[-1]["tools"] is None


@pytest.mark.parametrize(
    "text", ["", "What's on my screen?", "analyse this", "explain this error on the screen"]
)
async def test_screen_questions_go_straight_to_vision(tmp_path: Path, text: str) -> None:
    rig = Rig(tmp_path, ScriptedRouter(text_reply("A code editor with an error.", "Gemini")))
    reply = await rig.assistant.handle(text)
    request = rig.router.requests[0]
    assert request["vision"] is True
    parts = request["messages"][1]["content"]
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert reply.provider == "Gemini"
    assert rig.entries[-1].mode == "looked at screen"


async def test_model_can_choose_to_look_at_the_screen(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        ScriptedRouter(
            tool_reply("look_at_screen", question="Which app is open?"),
            text_reply("VS Code with a failing test.", "Gemini"),
            text_reply("You have VS Code open; one test is failing."),
        ),
    )
    reply = await rig.assistant.handle("can you help me with this test?")
    assert rig.router.requests[1]["vision"] is True
    assert "VS Code with a failing test." in rig.router.requests[2]["messages"][-1]["content"]
    assert reply.text == "You have VS Code open; one test is failing."
    assert rig.confirms == []  # looking is read-only


async def test_all_providers_failed_is_reported_honestly(tmp_path: Path) -> None:
    error = AllProvidersFailedError(
        Capability.CHAT,
        [Attempt("Groq", "timed out"), Attempt("Ollama", "Ollama isn't running")],
    )
    rig = Rig(tmp_path, ScriptedRouter(error))
    reply = await rig.assistant.handle("hello")
    assert not reply.ok and reply.provider is None
    assert "Groq: timed out" in reply.text
    assert rig.entries[-1].kind is EntryKind.ERROR
    assert rig.state.state is AppState.IDLE
    stored = await rig.store.messages(await rig.assistant.conversation_id())
    assert [m.role for m in stored] == ["user"]  # no fake assistant answer saved


@pytest.mark.parametrize("screen", [False, True])
def test_prompts_mark_external_text_as_data(screen: bool) -> None:
    from jarvis.agent.prompts import system_prompt

    assert "never instructions" in system_prompt(spoken=False, screen=screen)


@pytest.mark.parametrize(
    ("text", "screen"),
    [
        ("open notepad", False),
        ("what's the weather like", False),
        ("write an email to my boss", False),
        ("what's on my screen", True),
        ("Analyze this", True),
        ("what's this error", True),
    ],
)
def test_screen_intent(text: str, screen: bool) -> None:
    from jarvis.agent.prompts import wants_screen

    assert wants_screen(text) is screen
