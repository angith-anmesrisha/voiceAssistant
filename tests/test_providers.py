from __future__ import annotations

import json
import sys
from typing import Any

import httpx
import pytest

from jarvis.app.config import ProvidersConfig
from jarvis.providers.backends import build_providers
from jarvis.providers.base import Capability, ErrorKind, ProviderError, ProviderState


def make(handler: Any, monkeypatch: pytest.MonkeyPatch, **env: str) -> dict[str, Any]:
    for key in ("GROQ_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return build_providers(ProvidersConfig(), client)


def chat_reply(content: str | None = None, tool_calls: list[Any] | None = None) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content, "reasoning": "hidden"}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return httpx.Response(200, json={"model": "served-model", "choices": [{"message": message}]})


async def test_groq_chat_sends_openai_request_and_parses_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return chat_reply("Hello there.")

    groq = make(handler, monkeypatch, GROQ_API_KEY="gsk_test")["groq"]
    result = await groq.chat([{"role": "user", "content": "hi"}])
    assert seen["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert seen["auth"] == "Bearer gsk_test"
    assert seen["body"]["model"] == "qwen/qwen3.8-27b"
    assert seen["body"]["reasoning_effort"] == "none"  # thinking off for quick replies
    assert "tools" not in seen["body"]
    assert result.content == "Hello there."
    assert (result.provider, result.model) == ("Groq", "served-model")
    assert result.message == {"role": "assistant", "content": "Hello there."}  # no "reasoning"


async def test_tool_calls_are_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "open_app", "arguments": '{"name": "notepad"}'},
    }
    groq = make(lambda r: chat_reply(None, [call]), monkeypatch, GROQ_API_KEY="k")["groq"]
    result = await groq.chat([{"role": "user", "content": "open notepad"}], tools=[{"x": 1}])
    assert result.tool_calls[0].name == "open_app"
    assert result.tool_calls[0].arguments == {"name": "notepad"}
    assert result.message["tool_calls"][0]["function"]["arguments"] == '{"name": "notepad"}'


@pytest.mark.parametrize(
    ("status", "kind", "retryable"),
    [
        (401, ErrorKind.AUTH, False),
        (403, ErrorKind.AUTH, False),
        (429, ErrorKind.QUOTA, True),
        (400, ErrorKind.BAD_REQUEST, False),
        (503, ErrorKind.UNAVAILABLE, True),
    ],
)
async def test_http_errors_are_classified(
    monkeypatch: pytest.MonkeyPatch, status: int, kind: ErrorKind, retryable: bool
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "nope"}})

    groq = make(handler, monkeypatch, GROQ_API_KEY="k")["groq"]
    with pytest.raises(ProviderError) as info:
        await groq.chat([{"role": "user", "content": "hi"}])
    assert info.value.kind is kind
    assert info.value.retryable is retryable
    assert "nope" in info.value.message


async def test_timeout_and_network_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ProviderError) as info:
        await make(timeout, monkeypatch, GROQ_API_KEY="k")["groq"].chat([])
    assert info.value.kind is ErrorKind.TIMEOUT
    with pytest.raises(ProviderError) as info:
        await make(refused, monkeypatch, GROQ_API_KEY="k")["groq"].chat([])
    assert info.value.kind is ErrorKind.NETWORK


async def test_missing_key_never_calls_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return chat_reply("x")

    providers = make(handler, monkeypatch)
    assert providers["groq"].missing_config().startswith("no API key")
    assert (await providers["groq"].probe()).state is ProviderState.NO_KEY
    with pytest.raises(ProviderError) as info:
        await providers["gemini"].chat([])
    assert info.value.kind is ErrorKind.NO_KEY
    assert calls == []
    assert providers["ollama"].missing_config() is None  # local, no key needed


async def test_ollama_probe_reports_missing_model(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://localhost:11434/v1/models"
        return httpx.Response(200, json={"data": [{"id": "qwen2.5:7b"}]})

    status = await make(handler, monkeypatch)["ollama"].probe()
    assert status.state is ProviderState.MODEL_MISSING
    assert "ollama pull qwen3.5" in status.detail

    def with_model(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "vision"]})
        return httpx.Response(200, json={"data": [{"id": "qwen3.5:latest"}]})

    assert (await make(with_model, monkeypatch)["ollama"].probe()).state is ProviderState.READY


async def test_ollama_not_running(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    status = await make(refused, monkeypatch)["ollama"].probe()
    assert status.state is ProviderState.OFFLINE
    assert status.detail == "Ollama isn't running"


async def test_groq_transcribes_with_whisper(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/audio/transcriptions")
        assert b"whisper-large-v3-turbo" in request.content
        return httpx.Response(200, json={"text": " open notepad "})

    result = await make(handler, monkeypatch, GROQ_API_KEY="k")["groq"].transcribe(b"RIFF")
    assert (result.text, result.provider, result.model) == (
        "open notepad",
        "Groq",
        "whisper-large-v3-turbo",
    )


async def test_gemini_transcribes_through_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path.endswith("/v1beta/openai/chat/completions")
        assert body["messages"][0]["content"][1]["type"] == "input_audio"
        return chat_reply("what time is it")

    result = await make(handler, monkeypatch, GEMINI_API_KEY="k")["gemini"].transcribe(b"RIFF")
    assert result.text == "what time is it"
    assert result.provider == "Gemini"


async def test_gemini_bad_key_400_is_an_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json=[{"error": {"message": "API key not valid. Please pass a valid API key."}}]
        )

    gemini = make(handler, monkeypatch, GEMINI_API_KEY="bad")["gemini"]
    status = await gemini.probe()
    assert status.state is ProviderState.AUTH_ERROR
    with pytest.raises(ProviderError) as info:
        await gemini.chat([])
    assert info.value.kind is ErrorKind.AUTH


async def test_ollama_picks_an_installed_model_that_can_see(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shown: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            names = ["qwen3.5:latest", "llama3.2:latest", "gemma3:4b", "gemma3:1b"]
            return httpx.Response(200, json={"data": [{"id": n} for n in names]})
        assert str(request.url) == "http://localhost:11434/api/show"
        model = json.loads(request.content)["model"]
        shown.append(model)
        caps = ["completion", "vision"] if model.startswith("gemma3") else ["completion"]
        return httpx.Response(200, json={"capabilities": caps})

    ollama = make(handler, monkeypatch)["ollama"]
    assert (await ollama.probe()).state is ProviderState.READY
    # The preferred qwen3.5 can't see here, so the smallest model with vision wins.
    assert ollama.model_for(Capability.VISION) == "gemma3:1b"
    assert Capability.VISION in ollama.capabilities()
    assert Capability.STT not in ollama.capabilities()
    await ollama.probe()
    assert shown.count("gemma3:1b") == 1  # capabilities are cached


async def test_ollama_without_a_vision_model_cannot_analyze(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "qwen3.5:latest"}]})
        return httpx.Response(200, json={"capabilities": ["completion", "tools"]})

    ollama = make(handler, monkeypatch)["ollama"]
    assert (await ollama.probe()).state is ProviderState.READY
    assert Capability.VISION not in ollama.capabilities()


def test_keys_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.app import keys

    assert keys.get_key("GROQ_API_KEY") is None
    monkeypatch.setenv("GROQ_API_KEY", "  gsk_test  ")
    assert keys.get_key("GROQ_API_KEY") == "gsk_test"
    if sys.platform != "win32":  # on Windows this would write the real user environment
        assert keys.save_key("GEMINI_API_KEY", "abc") is False  # this session only
        assert keys.get_key("GEMINI_API_KEY") == "abc"


async def test_ollama_uses_qwen_without_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return chat_reply("Hi.")

    await make(handler, monkeypatch)["ollama"].chat([{"role": "user", "content": "hi"}])
    assert seen["model"] == "qwen3.5:latest"
    assert seen["reasoning_effort"] == "none"
