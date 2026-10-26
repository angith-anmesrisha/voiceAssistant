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
    assert (result.provider, result.model) == ("Ollama", "qwen3.5:4b")
    # qwen, then the three fallback Groq models; all busy, so the round is tried once more.
    assert calls.count("api.groq.com") == 8
    assert calls.count("generativelanguage.googleapis.com") == 1  # billing: no retry
    assert [a.provider for a in router.skipped] == ["Groq", "Gemini"]
    assert "FAILED_PRECONDITION" in router.skipped[1].error
    assert "Billing" in router.skipped[1].error  # says what to do about it
    assert changes == [
        "Asking Groq…",
        "Asking Groq again (2/2)…",
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
    assert [a.provider for a in caught.value.attempts] == [
        "Groq",
        "Gemini",
        "OpenRouter",  # no key: skipped without a request
        "Ollama",
    ]


async def test_gemini_switches_to_a_model_the_key_can_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        if body["model"] == "gemini-3.5-flash-lite":
            message = "models/gemini-3.5-flash-lite is not found for API version v1main"
            return httpx.Response(404, json=[{"error": {"message": message}}])
        if body["model"] == "gemini-3.8-flash":
            return httpx.Response(503, json=[{"error": {"message": "The model is overloaded."}}])
        return reply("Hello.", model=body["model"])

    gemini = build_providers(
        ProvidersConfig(), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )["gemini"]
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    result = await gemini.chat([{"role": "user", "content": "hi"}])
    # 3.5 Flash-Lite missing for this key, 3.8 Flash overloaded: 3.6 Flash answers.
    assert [b["model"] for b in sent] == [
        "gemini-3.5-flash-lite",
        "gemini-3.8-flash",
        "gemini-3.6-flash",
    ]
    assert result.model == "gemini-3.6-flash"
    assert sent[0]["reasoning_effort"] == "low"  # 3.x can't turn thinking off
    assert sent[0]["max_tokens"] == 2048
    assert gemini.model_for(Capability.CHAT) == "gemini-3.6-flash"
    await gemini.chat([{"role": "user", "content": "again"}])
    assert sent[3]["model"] == "gemini-3.6-flash"  # remembered: no failed call first


async def test_gemini_probe_picks_an_available_model(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        ids = ["models/gemini-3.8-flash", "models/gemini-3.6-flash"]
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
    assert window.notice.text() == "Gemini answered because Groq: timed out"
    window.quitting = True
    window.close()


def _attempt(provider: str, error: str) -> Any:
    from jarvis.providers.router import Attempt

    return Attempt(provider, error)


async def test_groq_rate_limit_switches_model_and_says_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, Any]] = []
    limit = (
        "Request too large for model `qwen/qwen3.8-27b` on tokens per minute (TPM): "
        "Limit 8000, Requested 9120"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        if body["model"] == "qwen/qwen3.8-27b":
            return httpx.Response(429, json={"error": {"message": limit}})
        return reply("Done.", model=body["model"])

    monkeypatch.setenv("GROQ_API_KEY", "g")
    groq = build_providers(
        ProvidersConfig(), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )["groq"]
    result = await groq.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])
    assert result.model == "minimaxai/minimax-m2.7"
    assert sent[0]["max_tokens"] == 2048 and sent[0]["reasoning_effort"] == "none"
    assert "reasoning_effort" not in sent[1]  # MiniMax has no such setting
    # The rate-limited model waits a minute before it is tried again.
    await groq.chat([{"role": "user", "content": "again"}])
    assert sent[2]["model"] == "minimaxai/minimax-m2.7"


async def test_all_groq_models_limited_names_each(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        return httpx.Response(429, json={"error": {"message": f"Rate limit reached for {model}"}})

    router = rig(handler, monkeypatch)
    router.set_preferred("groq")
    groq = router.provider("groq")
    assert groq is not None
    with pytest.raises(Exception) as caught:
        await groq.chat([{"role": "user", "content": "hi"}])
    text = str(caught.value)
    assert "qwen/qwen3.8-27b: HTTP 429" in text and "openai/gpt-oss-20b" in text


async def test_cut_off_reply_is_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        message = {"role": "assistant", "content": "Understood. Is"}
        choice = {"message": message, "finish_reason": "length"}
        return httpx.Response(200, json={"model": "qwen3.5:latest", "choices": [choice]})

    ollama = build_providers(
        ProvidersConfig(), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )["ollama"]
    result = await ollama.chat([{"role": "user", "content": "hi"}])
    assert result.content == "Understood. Is…"


async def test_a_timeout_does_not_try_every_model(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["model"])
        raise httpx.ReadTimeout("slow")

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    gemini = build_providers(
        ProvidersConfig(), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )["gemini"]
    with pytest.raises(Exception, match="timed out"):
        await gemini.chat([{"role": "user", "content": "hi"}])
    assert sent == ["gemini-3.5-flash-lite"]  # the next provider gets a turn instead
    # ... and the slow model waits five minutes: the next request starts on another one.
    sent.clear()
    with pytest.raises(Exception, match="timed out"):
        await gemini.chat([{"role": "user", "content": "again"}])
    assert sent == ["gemini-3.8-flash"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("test the AI providers", True),
        ("Jarvis, run diagnostics", True),
        ("check the models", True),
        ("test all the AIs", True),
        ("diagnose yourself", True),
        ("test my code", False),
        ("check the weather", False),
    ],
)
def test_diagnose_command(text: str, expected: bool) -> None:
    from jarvis.agent.commands import diagnose_command

    assert diagnose_command(text) is expected


async def test_diagnostics_report_says_whose_side(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.agent.assistant import diagnosis_report

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.5:4b"}]})
        if "groq" in host:
            message = "Rate limit reached for model `qwen/qwen3.8-27b`"
            return httpx.Response(429, json={"error": {"message": message}})
        if "googleapis" in host:
            return httpx.Response(400, json=GEMINI_BILLING)
        return reply("OK", model="qwen3.5:4b")

    router = rig(handler, monkeypatch)
    results = await router.diagnose()
    assert [(r.provider, r.ok, r.blame) for r in results] == [
        ("Groq", False, "account"),
        ("Gemini", False, "account"),
        ("OpenRouter", False, "setup"),  # no key
        ("Ollama", True, ""),
    ]
    text = diagnosis_report(results)
    assert "Groq: none of its models answered" in text and "Rate limit reached" in text
    assert "openai/gpt-oss-20b: HTTP 429" in text  # every Groq model was tried
    assert "your Gemini account, not JARVIS" in text
    assert "Ollama: working (qwen3.5:4b" in text


def test_whisper_guesses_on_silence_are_dropped() -> None:
    from jarvis.providers.openai_compat import heard_text

    silence = {
        "text": "I'm going to go.",
        "segments": [{"text": "I'm going to go.", "no_speech_prob": 0.82, "avg_logprob": -0.4}],
    }
    assert heard_text(silence) == ""
    speech = {
        "text": "Open Spotify.",
        "segments": [{"text": " Open Spotify.", "no_speech_prob": 0.02, "avg_logprob": -0.2}],
    }
    assert heard_text(speech) == "Open Spotify."
    assert heard_text({"text": " plain "}) == "plain"


async def test_self_test_finds_a_gemini_model_that_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jarvis.agent.assistant import diagnosis_report

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        host = request.url.host
        if host == "localhost":
            raise httpx.ConnectError("refused")
        if "groq" in host:
            return reply("OK", model="qwen/qwen3.8-27b")
        model = json.loads(request.content)["model"]
        if model == "gemini-3.5-flash-lite":
            raise httpx.ReadTimeout("slow")
        return reply("OK", model=model)

    router = rig(handler, monkeypatch)
    results = await router.diagnose()
    gemini = results[1]
    assert gemini.ok and gemini.model == "gemini-3.8-flash"
    assert gemini.tried == (("gemini-3.5-flash-lite", "timed out"),)
    provider = router.provider("gemini")
    assert provider is not None
    assert provider.model_for(Capability.CHAT) == "gemini-3.8-flash"  # used from now on
    text = diagnosis_report(results)
    assert "Gemini: working (gemini-3.8-flash" in text
    assert "because gemini-3.5-flash-lite: timed out" in text


def test_settings_can_change_just_the_local_model(tmp_path: Any) -> None:
    from jarvis.app.config import load_settings

    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {"providers": {"ollama": {"chat_model": "qwen3.5:4b", "vision_model": "qwen3.5:4b"}}}
        )
    )
    settings = load_settings(path)
    assert settings.providers.ollama.chat_model == "qwen3.5:4b"
    assert settings.providers.ollama.base_url == "http://localhost:11434/v1"  # kept
    assert settings.providers.groq.chat_model == "qwen/qwen3.8-27b"


async def test_a_model_rejecting_the_request_hands_over_to_the_next_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        models.append(model)
        if model == "qwen/qwen3.8-27b":
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "Failed to call a function. Please adjust your prompt.",
                        "code": "tool_use_failed",
                    }
                },
            )
        return reply("Done.", model=model)

    router = rig(handler, monkeypatch)
    result = await router.chat([{"role": "user", "content": "hi"}])
    assert result.provider == "Groq"
    assert models == ["qwen/qwen3.8-27b", "minimaxai/minimax-m2.7"]


async def test_vision_failure_names_the_provider_without_a_vision_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "groq" in request.url.host:
            return httpx.Response(429, json={"error": {"message": "rate limit"}})
        if "googleapis" in request.url.host:
            return httpx.Response(503, json={"error": {"message": "overloaded"}})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "llama3.2:3b"}]})
        return httpx.Response(200, json={"capabilities": ["completion"]})

    router = rig(handler, monkeypatch)
    ollama = router.provider("ollama")
    assert ollama is not None
    await ollama.probe()
    with pytest.raises(AllProvidersFailedError) as caught:
        await router.chat([{"role": "user", "content": "look"}], vision=True)
    assert [a.provider for a in caught.value.attempts][-1] == "Ollama"
    assert "can see images" in caught.value.attempts[-1].error


def test_openrouter_picks_a_free_model_that_fits() -> None:
    from jarvis.providers.backends import pick_free_model

    models: list[dict[str, Any]] = [
        {"id": "moonshotai/kimi-k3", "supported_parameters": ["tools"]},
        {"id": "a/no-tools:free", "supported_parameters": []},
        {"id": "b/text:free", "supported_parameters": ["tools"]},
        {
            "id": "c/sees:free",
            "supported_parameters": ["tools"],
            "architecture": {"input_modalities": ["text", "image"]},
        },
    ]
    assert pick_free_model(models) == "b/text:free"
    assert pick_free_model(models, vision=True) == "c/sees:free"
    assert pick_free_model([]) == ""


def test_old_saved_priority_gains_openrouter() -> None:
    old = ProvidersConfig.model_validate({"priority": ["groq", "gemini", "ollama"]})
    assert old.priority == ["groq", "gemini", "openrouter", "ollama"]
    custom = ProvidersConfig.model_validate({"priority": ["ollama", "groq"]})
    assert custom.priority == ["ollama", "groq"]


def test_cloud_first_and_all_the_free_models() -> None:
    from jarvis.app.config import RoutingSettings
    from jarvis.providers.backends import OpenRouterProvider

    assert RoutingSettings().local_first is False  # the local model was too slow
    config = ProvidersConfig.model_validate(
        {"groq": {"fallback_models": ["llama-3.3-70b-versatile", "openai/gpt-oss-120b"]}}
    )
    assert config.groq.fallback_models == ["openai/gpt-oss-120b"]  # shut down on Groq
    assert config.priority == ["groq", "gemini", "openrouter", "ollama"]
    provider = OpenRouterProvider(
        "openrouter", "OpenRouter", ProvidersConfig().openrouter, httpx.AsyncClient()
    )
    listed = {
        "data": [
            {"id": "paid/model", "supported_parameters": ["tools"]},
            {"id": "new/free-one:free", "supported_parameters": ["tools"]},
            {"id": "no/tools:free", "supported_parameters": []},
        ]
    }
    request = httpx.Request("GET", "https://openrouter.ai/api/v1/models")
    provider._check_models(httpx.Response(200, json=listed, request=request), 10)
    fallbacks = provider.fallbacks_for(Capability.CHAT)
    assert fallbacks[-1] == "new/free-one:free" and "paid/model" not in fallbacks
    assert all(m.endswith(":free") or m == "openrouter/free" for m in fallbacks)
