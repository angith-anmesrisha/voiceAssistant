"""The assistant: builds context, calls the provider router, runs tools safely."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger

from jarvis.agent.prompts import system_prompt, wants_screen
from jarvis.app.events import ChatEntry, EntryKind, EventBus
from jarvis.app.state import AppState, StateMachine
from jarvis.providers.base import Capability, ChatResult, Message, ToolCall
from jarvis.providers.openai_compat import image_part
from jarvis.providers.router import AllProvidersFailedError, ProviderRouter
from jarvis.storage.db import HistoryStore
from jarvis.tools.registry import ArgumentError, Risk, ToolRegistry, ToolResult, validate_args


@dataclass(frozen=True, slots=True)
class ConfirmRequest:
    tool: str
    risk: Risk
    description: str


LOOKED = "looked at screen"
ACTED = "used tools"

LOOK_AT_SCREEN: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "look_at_screen",
        "description": (
            "Take a screenshot of the user's screen (JARVIS hides itself) and get a "
            "description of it. Use when the request is about something on screen."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "What to look for or answer."}
            },
            "required": [],
        },
    },
}

ConfirmFn = Callable[[ConfirmRequest], Awaitable[bool]]
ScreenCaptureFn = Callable[[], Awaitable[bytes | None]]


@dataclass(frozen=True, slots=True)
class Reply:
    text: str
    ok: bool
    provider: str | None = None
    model: str | None = None


class Assistant:
    def __init__(
        self,
        router: ProviderRouter,
        store: HistoryStore,
        registry: ToolRegistry,
        bus: EventBus,
        state: StateMachine,
        confirm: ConfirmFn,
        capture_screen: ScreenCaptureFn,
        *,
        history_messages: int = 20,
        max_tool_rounds: int = 5,
    ) -> None:
        self._router = router
        self._store = store
        self._registry = registry
        self._bus = bus
        self._state = state
        self._confirm = confirm
        self._capture_screen = capture_screen
        self._history_messages = history_messages
        self._max_tool_rounds = max_tool_rounds
        self._conversation_id: int | None = None
        self._did: set[str] = set()

    async def conversation_id(self) -> int:
        if self._conversation_id is None:
            self._conversation_id = await self._store.latest_or_new_conversation()
        return self._conversation_id

    async def new_conversation(self) -> None:
        self._conversation_id = await self._store.new_conversation()

    async def _context(self) -> list[Message]:
        """Recent user/assistant text turns (tool traffic is not replayed)."""
        stored = await self._store.messages(await self.conversation_id(), limit=200)
        turns = [
            {"role": m.role, "content": m.content}
            for m in stored
            if m.role in ("user", "assistant") and m.content and not m.payload.get("tool_calls")
        ]
        return turns[-self._history_messages :]

    async def _emit(self, kind: EntryKind, text: str, **meta: str | None) -> None:
        await self._bus.publish(ChatEntry(kind=kind, text=text, **meta))

    async def handle(self, text: str, *, spoken: bool = False) -> Reply:
        """Answer one request. Whether to look, act or just answer is decided here."""
        text = text.strip()
        conversation = await self.conversation_id()
        shown = text or "What's on my screen?"
        await self._emit(EntryKind.USER, shown)
        await self._store.add_message(conversation, {"role": "user", "content": shown})
        await self._state.transition(AppState.THINKING)
        self._did = set()
        try:
            if wants_screen(text):
                self._did.add(LOOKED)
                result = await self._look(shown, spoken)
            else:
                result = await self._converse(shown, spoken)
        except AllProvidersFailedError as exc:
            logger.warning("All providers failed: {}", exc)
            message = _failure_message(exc)
            await self._emit(EntryKind.ERROR, message)
            return Reply(message, ok=False)
        except _NoScreenError:
            message = "I couldn't capture the screen."
            await self._emit(EntryKind.ERROR, message)
            return Reply(message, ok=False)
        finally:
            if self._state.state in (AppState.THINKING, AppState.ACTING):
                await self._state.transition(AppState.IDLE)

        reply = result.content or "Done."
        label = " + ".join(sorted(self._did)) or None
        await self._store.add_message(
            conversation,
            {"role": "assistant", "content": reply},
            provider=result.provider,
            model=result.model,
            mode=label,
        )
        await self._emit(
            EntryKind.ASSISTANT, reply, provider=result.provider, model=result.model, mode=label
        )
        return Reply(reply, ok=True, provider=result.provider, model=result.model)

    async def _look(self, question: str, spoken: bool) -> ChatResult:
        jpeg = await self._capture_screen()
        if jpeg is None:
            raise _NoScreenError
        messages: list[Message] = [
            {"role": "system", "content": system_prompt(spoken=spoken, screen=True)},
            {"role": "user", "content": [{"type": "text", "text": question}, image_part(jpeg)]},
        ]
        return await self._router.chat(messages, vision=True)

    async def _converse(self, text: str, spoken: bool) -> ChatResult:
        messages: list[Message] = [
            {"role": "system", "content": system_prompt(spoken=spoken)},
            *await self._context(),
        ]
        if not messages[-1:] or messages[-1].get("content") != text:
            messages.append({"role": "user", "content": text})

        tools = [*self._registry.declarations(), LOOK_AT_SCREEN]
        for _round in range(self._max_tool_rounds):
            result = await self._router.chat(messages, tools=tools)
            if not result.tool_calls:
                return result
            messages.append(result.message)
            for call in result.tool_calls:
                if call.name == LOOK_AT_SCREEN["function"]["name"]:
                    outcome = await self._look_tool(call, spoken)
                else:
                    outcome = await self._run_tool(call)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": json.dumps(outcome.as_payload()),
                    }
                )
            await self._state.transition(AppState.THINKING)
        # Out of rounds: ask for a final answer without tools.
        messages.append(
            {"role": "user", "content": "Stop using tools now and tell me what was done."}
        )
        return await self._router.chat(messages)

    async def _look_tool(self, call: ToolCall, spoken: bool) -> ToolResult:
        """The model asked to see the screen: a vision model describes it as text."""
        question = str(call.arguments.get("question") or "Describe what is on the screen.")
        self._did.add(LOOKED)
        try:
            seen = await self._look(question, spoken)
        except _NoScreenError:
            return ToolResult(False, "The screen couldn't be captured.")
        except AllProvidersFailedError as exc:
            return ToolResult(False, _failure_message(exc))
        return ToolResult(True, seen.content or "Nothing notable on screen.")

    async def _run_tool(self, call: ToolCall) -> ToolResult:
        spec = self._registry.get(call.name)
        if spec is None:
            return ToolResult(False, f"There is no tool called {call.name}.")
        try:
            args = validate_args(spec.parameters, call.arguments)
        except ArgumentError as exc:
            return ToolResult(False, f"Invalid arguments: {exc}.")
        risk = spec.risk_for(args)
        description = spec.describe(args)
        if risk >= Risk.MEDIUM:
            approved = await self._confirm(ConfirmRequest(spec.name, risk, description))
            if not approved:
                await self._emit(EntryKind.ACTION, f"Declined: {description}")
                return ToolResult(False, "The user declined this action.")
        await self._state.transition(AppState.ACTING)
        self._did.add(ACTED)
        try:
            result = await self._registry.run(spec, args)
        except Exception as exc:
            logger.exception("Tool {} crashed", spec.name)
            result = ToolResult(False, f"The tool failed ({type(exc).__name__}).")
        logger.info("Tool {} -> ok={}", spec.name, result.ok)
        await self._emit(EntryKind.ACTION, ("✓ " if result.ok else "✕ ") + result.summary)
        return result


class _NoScreenError(Exception):
    pass


def _failure_message(exc: AllProvidersFailedError) -> str:
    if exc.capability is Capability.VISION and not exc.attempts:
        return (
            "I can't analyze the screen yet: that needs a model that can see images. Add a "
            "Gemini key, or install one in Ollama (for example: ollama pull qwen3.5:4b)."
        )
    if not exc.attempts:
        return (
            "I couldn't reach any AI provider. None is set up: add a Groq or Gemini key "
            "(key button), or start Ollama."
        )
    detail = " ".join(f"{a.provider}: {a.error}." for a in exc.attempts)
    return f"I couldn't reach any AI provider. {detail}"
