"""Z.ai (GLM models), for the morning brief.

Owner, 2026-10-29: "use z.ai to give me a morning brief". Z.ai's API is OpenAI-shaped
(``POST {base}/chat/completions``, ``Authorization: Bearer <key>``; docs.z.ai). The key
is ``ZAI_API_KEY``, saved like the other API keys (user environment) and never logged.
The general endpoint is tried first, then the GLM Coding Plan one, since a Coding Plan
key only works there. Models come from settings (``brief.models``) and are tried in
order. What is sent to Z.ai is processed by Z.ai, an external service.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from loguru import logger

from jarvis.app.keys import get_key

KEY = "ZAI_API_KEY"
BASES = ("https://api.z.ai/api/paas/v4", "https://api.z.ai/api/coding/paas/v4")
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class ZaiError(RuntimeError):
    pass


@dataclass(frozen=True)
class Written:
    text: str
    model: str  # the Z.ai model that really wrote it


def _problem(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        code = error.get("code")
        message = str(error.get("message") or "")
        return (f"{code}: " if code else "") + message[:200]
    return f"HTTP {response.status_code}"


class ZaiWriter:
    def __init__(
        self,
        client: httpx.AsyncClient,
        models: list[str],
        *,
        key: Callable[[str], str | None] = get_key,
        timeout_s: float = 240.0,
        bases: tuple[str, ...] = BASES,
    ) -> None:
        self._client = client
        self.models = [m for m in models if m.strip()]
        self._key = key
        self._timeout = timeout_s
        self._bases = bases

    @property
    def ready(self) -> bool:
        return bool((self._key(KEY) or "").strip())

    async def write(self, system: str, user: str, *, max_tokens: int = 12_000) -> Written:
        key = (self._key(KEY) or "").strip()
        if not key:
            raise ZaiError("the Z.ai key isn't set up. Add it in the Morning brief node's settings")
        failures: list[str] = []
        for base in self._bases:
            for model in self.models:
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": 0.3,
                }
                try:
                    response = await self._client.post(
                        f"{base}/chat/completions",
                        json=payload,
                        headers={"Authorization": f"Bearer {key}"},
                        timeout=self._timeout,
                    )
                except httpx.HTTPError as exc:  # never str(exc) with headers around
                    failures.append(f"{model}: {type(exc).__name__}")
                    continue
                if response.status_code == 401:
                    failures.append(f"{model}: the key wasn't accepted")
                    break  # the same key fails for every model on this endpoint
                if response.status_code != 200:
                    failures.append(f"{model}: {_problem(response)}")
                    continue
                try:
                    choice = response.json()["choices"][0]
                    text = str(choice["message"].get("content") or "")
                except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                    failures.append(f"{model}: an answer JARVIS couldn't read")
                    continue
                text = _THINK.sub("", text).strip()
                if not text:
                    failures.append(f"{model}: an empty answer")
                    continue
                if choice.get("finish_reason") == "length":
                    text += "\n\n…"
                logger.info("Z.ai {} wrote {} chars", model, len(text))
                return Written(text, model)
        logger.warning("Z.ai didn't write it: {}", "; ".join(failures)[:400])
        raise ZaiError("Z.ai didn't answer (" + "; ".join(dict.fromkeys(failures))[:300] + ")")
