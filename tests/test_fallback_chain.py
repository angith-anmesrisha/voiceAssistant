"""Groq -> Gemini -> Ollama fallback with the real providers, Gemini model fallback, labels."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from PySide6.QtWidgets import QApplication

from jarvis.app.config import ProvidersConfig
from jarvis.app.state import AppState
from jarvis.providers.backends import build_providers
from jarvis.providers.base import Capability, ProviderState, ProviderStatus
from jarvis.providers.network import NetworkStatus
from jarvis.providers.router import AllProvidersFailedError, ProviderRouter
from tests.test_controller_and_ui import qapp

__all__ = ["qapp"]

GEMINI_BILLING = [
    {
        "error": {
            "code": 400,
            "message": "Precondition check failed.",
            "status": "FAILED_PRECONDITION",
        }
    }
]


def reply(content: str = "", model: str = "served", **message: Any) -> httpx.Response:
    body = {"role": "assistant", "content": content, **message}
    return httpx.Response(200, json={"model": model, "choices": [{"message": body}]})


async def online() -> NetworkStatus:
    return NetworkStatus(online=True)


def rig(
    handler: Any, monkeypatch: pytest.MonkeyPatch, changes: list[str] | None = None
) -> ProviderRouter:
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    config = ProvidersConfig()
    providers = build_providers(config, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    router: ProviderRouter

    def changed() -> None:
        if changes is not None and router.activity and changes[-1:] != [router.activity]:
            changes.append(router.activity)

    router = ProviderRouter(
        providers,
        config.priority,
        {name: config.get(name).max_retries for name in providers},
        backoff_s=0,
        network_check=online,
        on_change=changed,
    )
    return router


async def test_groq_then_gemini_then_local_qwen(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        calls.append(host)
        if "groq" in host:
            return httpx.Response(503, json={"error": {"message": "over capacity"}})
        if "googleapis" in host:
            return httpx.Response(400, json=GEMINI_BILLING)
        body = json.loads(request.content)
        return reply("Hi from your PC.", model=body["model"])

    changes: list[str] = []
    router = rig(handler, monkeypatch, changes)
    result = await router.chat([{"role": "user", "content": "hi"}])
    assert (result.provider, result.model) == ("Ollama", "qwen3.5:latest")
    assert calls.count("api.groq.com") == 3  # 1 try + 2 retries on a 503
    assert calls.count("generativelanguage.googleapis.com") == 1  # billing: no retry
    assert [a.provider for a in router.skipped] == ["Groq", "Gemini"]
    assert "FAILED_PRECONDITION" in router.skipped[1].error
    assert "Billing" in router.skipped[1].error  # says what to do about it
    assert changes == [
        "Asking Groq…",
        "Asking Groq again (2/3)…",
        "Asking Groq again (3/3)…",
        "Groq didn't answer, asking Gemini…",
        "Gemini didn't answer, asking Ollama…",
    ]
    assert router.activity == ""


async def test_all_three_failing_lists_each_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "localhost":
            raise httpx.ConnectError("refused")
        return httpx.Response(401, json={"error": {"message": "Invalid API Key"}})

    router = rig(handler, monkeypatch)
    with pytest.raises(AllProvidersFailedError) as caught:
        await router.chat([{"role": "user", "content": "hi"}])
    assert [a.provider for a in caught.value.attempts] == ["Groq", "Gemini", "Ollama"]


async def test_gemini_switches_to_a_model_the_key_can_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        if body["model"] == "gemini-2.5-flash":
            message = "models/gemini-2.5-flash is not found for API version v1main"
            return httpx.Response(404, json=[{"error": {"message": message}}])
        return reply("Hello.", model=body["model"])

    gemini = build_providers(
        ProvidersConfig(), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )["gemini"]
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    result = await gemini.chat([{"role": "user", "content": "hi"}])
    assert result.model == "gemini-3.8-flash"
    assert sent[0]["reasoning_effort"] == "none"  # 2.5 Flash: thinking off
    assert sent[1]["reasoning_effort"] == "low"  # 3.x can't turn thinking off
    assert gemini.model_for(Capability.CHAT) == "gemini-3.8-flash"
    await gemini.chat([{"role": "user", "content": "again"}])
    assert sent[2]["model"] == "gemini-3.8-flash"  # remembered: no failed call first


async def test_gemini_probe_picks_an_available_model(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        ids = ["models/gemini-3.8-flash", "models/gemini-3.5-flash-lite"]
        return httpx.Response(200, json={"data": [{"id": i} for i in ids]})

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    gemini = build_providers(
        ProvidersConfig(), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )["gemini"]
    status = await gemini.probe()
    assert status.state is ProviderState.READY
    assert gemini.model_for(Capability.CHAT) == "gemini-3.8-flash"
    assert gemini.model_for(Capability.STT) == "gemini-3.8-flash"


async def test_thought_signatures_survive_only_where_understood(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: dict[str, dict[str, Any]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent[request.url.host] = json.loads(request.content)
        return reply("ok")

    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    providers = build_providers(
        ProvidersConfig(), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    signed = {"google": {"thought_signature": "abc"}}
    history: list[Any] = [
        {"role": "user", "content": "open notepad"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "open_app", "arguments": "{}"},
                    "extra_content": signed,
                },
                {"id": "2", "type": "function", "function": {"name": "x", "arguments": "{}"}},
            ],
        },
    ]
    await providers["groq"].chat(history)
    await providers["gemini"].chat(history)
    groq_calls = sent["api.groq.com"]["messages"][1]["tool_calls"]
    gemini_calls = sent["generativelanguage.googleapis.com"]["messages"][1]["tool_calls"]
    assert all("extra_content" not in c for c in groq_calls)
    assert gemini_calls[0]["extra_content"] == signed
    assert gemini_calls[1]["extra_content"]["google"]["thought_signature"]


async def test_ui_names_the_provider_actually_used(qapp: QApplication) -> None:
    from jarvis.app.events import ChatEntry, EntryKind
    from jarvis.ui.main_window import MainWindow
    from tests.test_router import FakeProvider, make

    router = make(FakeProvider("groq"), FakeProvider("gemini"))
    router.statuses["groq"] = ProviderStatus(ProviderState.ERROR, "timed out")
    router.statuses["gemini"] = ProviderStatus(ProviderState.READY)
    window = MainWindow()
    window._router = router
    window.refresh_providers()
    # Groq errored once but is still tried first, so the header must not claim Gemini.
    assert "GEMINI" not in window.header._status_text
    window.set_app_state(AppState.THINKING)
    assert window.caption._hint == "Asking Groq…"
    router.activity = "Groq didn't answer, asking Gemini…"
    await window._on_providers_event(None)  # type: ignore[arg-type]
    assert window.caption._hint == "Groq didn't answer, asking Gemini…"
    router.activity = ""
    router.last_used = ("Gemini", "gemini-3.8-flash")
    router.skipped = [_attempt("Groq", "timed out")]
    window.add_entry(ChatEntry(EntryKind.ASSISTANT, "Hi.", "Gemini", "gemini-3.8-flash"))
    window.refresh_providers()
    assert window.header._status_text == "AI ONLINE · GEMINI"
    assert "Groq didn't answer (timed out); Gemini did." in window.notice.text()
    window.quitting = True
    window.close()


def _attempt(provider: str, error: str) -> Any:
    from jarvis.providers.router import Attempt

    return Attempt(provider, error)
