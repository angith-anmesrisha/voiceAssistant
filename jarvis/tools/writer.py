"""Long writing jobs (document summaries, research reports, drafts) on free models.

Long inputs don't fit Groq's free tier (8K tokens a minute), so these go to the free
OpenRouter models first (the same list as the slide decks, ``slides.models``), then
Groq when the prompt is short enough, then, for prompts that still fit, the normal
provider chain. The result names the model that actually wrote it.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from jarvis.providers.base import ProviderError
from jarvis.tools.slides import GROQ_PROMPT_CHARS, is_free

Messages = list[dict[str, Any]]


class WriterError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Written:
    content: str
    provider: str
    model: str

    @property
    def credit(self) -> str:
        """ "nemotron-3-super-120b-a12b (OpenRouter, free)"."""
        name = self.model.split("/")[-1].removesuffix(":free")
        free = ", free" if is_free(self.model) or self.provider == "Groq" else ""
        return f"{name} ({self.provider}{free})"


class FreeWriter:
    def __init__(
        self,
        openrouter: Any,
        groq: Any,
        models: list[str],
        *,
        groq_model: str = "openai/gpt-oss-120b",
        timeout_s: float = 240.0,
        notify: Callable[[str], None] | None = None,
        fallback: Callable[[Messages], Awaitable[Any]] | None = None,
        fallback_chars: int = 24_000,
    ) -> None:
        self._openrouter = openrouter
        self._groq = groq
        self._models = [m for m in models if is_free(m)]
        self._groq_model = groq_model
        self._timeout_s = timeout_s
        self._notify = notify or (lambda _text: None)
        self._fallback = fallback
        self._fallback_chars = fallback_chars
        self._gone: set[str] = set()

    def _attempts(self, prompt_chars: int, max_tokens: int) -> list[tuple[Any, str, int]]:
        attempts: list[tuple[Any, str, int]] = []
        if self._openrouter is not None and self._openrouter.missing_config() is None:
            attempts += [
                (self._openrouter, m, max_tokens) for m in self._models if m not in self._gone
            ]
        groq_ready = self._groq is not None and self._groq.missing_config() is None
        if groq_ready and self._groq_model and prompt_chars <= GROQ_PROMPT_CHARS:
            attempts.append((self._groq, self._groq_model, min(max_tokens, 4_000)))
        return attempts

    async def write(self, messages: Messages, *, label: str, max_tokens: int = 8_000) -> Written:
        prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
        problems: list[str] = []
        for provider, model, tokens in self._attempts(prompt_chars, max_tokens):
            name = model.split("/")[-1].removesuffix(":free")
            self._notify(f"{label} with {name} ({provider.display_name}, free)…")
            try:
                result = await provider.complete(
                    model, messages, max_tokens=tokens, timeout_s=self._timeout_s
                )
            except ProviderError as exc:
                logger.warning("Writer: {} failed: {}", model, exc.message[:200])
                if exc.message.startswith("HTTP 404") or (
                    exc.message.startswith("HTTP 403") and "available" in exc.message.lower()
                ):
                    self._gone.add(model)
                problems.append(f"{name}: {exc.message[:120]}")
                continue
            text = strip_thinking(result.content or "")
            if text:
                return Written(text, result.provider, result.model)
            problems.append(f"{name}: an empty answer")
        if self._fallback is not None and prompt_chars <= self._fallback_chars:
            try:
                result = await self._fallback(messages)
            except Exception as exc:  # AllProvidersFailedError and friends
                problems.append(str(exc)[:160])
            else:
                text = strip_thinking(result.content or "")
                if text:
                    return Written(text, result.provider, result.model)
        if not problems:
            raise WriterError(
                "long writing uses free OpenRouter models and there's no OPENROUTER_API_KEY "
                "yet (free at openrouter.ai/settings/keys; add it with the key button under "
                "AI PROVIDERS)"
            )
        raise WriterError("no model could write it. " + "; ".join(problems[-4:]))


def strip_thinking(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()


def unique_file(folder: Path, title: str, suffix: str) -> Path:
    """A new file name in ``folder`` from ``title`` (never an existing file)."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", title).strip(" .")[:80] or "Untitled"
    name = re.sub(r"\s+", " ", name)
    path = folder / f"{name}{suffix}"
    count = 2
    while path.exists():
        path = folder / f"{name} ({count}){suffix}"
        count += 1
    return path
