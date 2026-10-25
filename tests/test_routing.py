"""Local-first routing: conversation on the local models (Qwen 3.5 4B, then Gemma 3 4B),
the cloud for actions, the screen and anything current, and the cloud again when the
local models can't answer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from jarvis.agent.assistant import Assistant
from jarvis.agent.routing import KeepLocalWarm, is_conversation, wants_cloud
from jarvis.agent.toolbudget import groups_for
from jarvis.app.config import ProvidersConfig, ToolSettings
from jarvis.providers.backends import OllamaProvider
from jarvis.providers.base import (
    ChatResult,
    ErrorKind,
    ProviderError,
    ProviderState,
    ProviderStatus,
)
from jarvis.providers.router import ProviderRouter
from jarvis.tools.apps import AppIndex
from jarvis.tools.builtin import build_registry
from tests.test_assistant import Rig, ScriptedRouter, text_reply


@pytest.mark.parametrize(
    "text",
    [
        "Capital of France?",
        "Explain how a jet engine works",
        "Write me a short poem about the monsoon",
        "What's a good way to learn guitar as an adult?",
        "I'm feeling a bit stressed about exams",
        "tell me a fun fact about octopuses",  # "tell me about" is the info group; this isn't
    ],
)
def test_conversation_stays_local(text: str) -> None:
    assert is_conversation(text, groups_for(text))


@pytest.mark.parametrize(
    "text",
    [
        "open Spotify",
        "Jarvis, can you set the volume to 30",
        "what's the weather tomorrow",
        "latest news on the election",
        "remind me at 6 to call Mom",
        "what's on my screen",
        "summarise the PDF in my downloads",
        "what's the Nifty at",
        "make a 10 slide presentation on black holes",
        "search for cheap flights to Delhi",
    ],
)
def test_actions_and_current_things_go_to_the_cloud(text: str) -> None:
    assert not is_conversation(text, groups_for(text))


def test_a_handover_from_the_local_model_is_seen() -> None:
    assert wants_cloud("NEEDS_TOOLS") and wants_cloud(" `NEEDS_TOOLS`.") and wants_cloud("")
    assert not wants_cloud("Paris is the capital of France.")


class LocalRouter(ScriptedRouter):
    def __init__(self, local: list[ChatResult | Exception], *cloud: ChatResult) -> None:
        super().__init__(*cloud)
        self.local = local
        self.local_requests: list[list[Any]] = []
        self.ready = True

    def local_ready(self) -> bool:
        return self.ready

    async def local_chat(self, messages: list[Any], *, timeout_s: float) -> ChatResult:
        self.local_requests.append(messages)
        result = self.local.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def local_rig(tmp_path: Path, router: LocalRouter) -> Rig:
    rig = Rig(tmp_path, router)
    rig.assistant = Assistant(
        router,  # type: ignore[arg-type]
        rig.store,
        build_registry(ToolSettings(), rig.desktop, AppIndex([], apps_folder=False)),
        rig.bus,
        rig.state,
        lambda _r: _yes(),
        lambda: _none(),
        local_first=lambda: True,
    )
    return rig


async def _yes() -> bool:
    return True


async def _none() -> bytes | None:
    return None


async def test_conversation_is_answered_locally_without_tools(tmp_path: Path) -> None:
    router = LocalRouter([text_reply("Paris.", provider="Ollama")])
    rig = local_rig(tmp_path, router)
    reply = await rig.assistant.handle("Capital of France?")
    assert (reply.text, reply.provider) == ("Paris.", "Ollama")
    assert router.requests == []  # no cloud call
    system = router.local_requests[0][0]["content"]
    assert "NEEDS_TOOLS" in system and "open_app" not in system  # the short prompt


async def test_the_cloud_answers_when_local_hands_over_or_fails(tmp_path: Path) -> None:
    down = ProviderError("Ollama", ErrorKind.TIMEOUT, "no answer within 90 s")
    router = LocalRouter(
        [text_reply("NEEDS_TOOLS", provider="Ollama"), down],
        text_reply("It's sunny.", provider="Groq"),
        text_reply("Paris.", provider="Groq"),
    )
    rig = local_rig(tmp_path, router)
    assert (await rig.assistant.handle("Is it nice to visit Goa in June?")).provider == "Groq"
    assert (await rig.assistant.handle("Capital of France?")).provider == "Groq"
    assert router.requests[0]["tools"]  # the cloud gets the tools as before


async def test_actions_skip_the_local_models(tmp_path: Path) -> None:
    router = LocalRouter([], text_reply("Opened.", provider="Groq"))
    rig = local_rig(tmp_path, router)
    await rig.assistant.handle("open Spotify")
    assert router.local_requests == [] and router.requests


async def test_no_local_model_running_means_the_cloud(tmp_path: Path) -> None:
    router = LocalRouter([], text_reply("Paris.", provider="Groq"))
    router.ready = False
    rig = local_rig(tmp_path, router)
    assert (await rig.assistant.handle("Capital of France?")).provider == "Groq"


async def test_router_asks_qwen_then_gemma_on_this_pc() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        if body["model"] == "qwen3.5:4b":
            return httpx.Response(500, json={"error": {"message": "out of memory"}})
        return httpx.Response(
            200, json={"model": body["model"], "choices": [{"message": {"content": "Hi."}}]}
        )

    config = ProvidersConfig()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ollama = OllamaProvider("ollama", "Ollama", config.ollama, client)
        router = ProviderRouter({"ollama": ollama}, ["ollama"], {"ollama": 0})
        router.statuses["ollama"] = ProviderStatus(ProviderState.READY)
        assert router.local_ready()
        result = await router.local_chat([{"role": "user", "content": "hello"}], timeout_s=5)
        router.set_preferred("ollama")
        assert router.local_ready()
    assert (result.provider, result.model) == ("Ollama", "gemma3:4b")
    assert [b["model"] for b in sent] == ["qwen3.5:4b", "gemma3:4b"]
    assert sent[0]["reasoning_effort"] == "none" and "reasoning_effort" not in sent[1]
    assert "tools" not in sent[1]
    assert router.last_used == ("Ollama", "gemma3:4b")


def test_a_chosen_cloud_provider_wins_over_local() -> None:
    config = ProvidersConfig()
    ollama = OllamaProvider("ollama", "Ollama", config.ollama, httpx.AsyncClient())
    groq = OllamaProvider("groq", "Groq", config.groq, httpx.AsyncClient())
    router = ProviderRouter({"groq": groq, "ollama": ollama}, ["groq", "ollama"], {})
    router.statuses["ollama"] = ProviderStatus(ProviderState.READY)
    assert router.local_ready()
    router.set_preferred("groq")
    assert not router.local_ready()


def test_old_saved_ollama_settings_get_gemma() -> None:
    old = ProvidersConfig.model_validate({"ollama": {"fallback_models": ["qwen3.5:latest"]}})
    assert old.ollama.fallback_models[0] == "gemma3:4b"


async def test_the_local_model_is_kept_loaded() -> None:
    from datetime import datetime, timedelta

    warmed: list[int] = []

    async def warm() -> bool:
        warmed.append(1)
        return True

    job = KeepLocalWarm(lambda: True, warm, every_s=600)
    start = datetime(2026, 10, 25, 9).astimezone()
    for minutes in (0, 5, 11):
        await job.tick(start + timedelta(minutes=minutes))
    assert len(warmed) == 2
