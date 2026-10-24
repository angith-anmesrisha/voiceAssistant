"""Optional link to a Hindsight memory server (github.com/vectorize-io/hindsight, MIT).

Hindsight needs Postgres, an embedding model, a reranker and an LLM call per memory, so
JARVIS doesn't bundle it: the Markdown vault covers memory on its own. If you already
run a Hindsight server (``memory.hindsight_url``), JARVIS also sends it each exchange
and merges its recall into the vault's. Only its documented REST API is used:

- ``PUT  /v1/default/banks/{bank}`` creates the memory bank;
- ``POST /v1/default/banks/{bank}/memories`` with ``{"items": [{"content", "context",
  "timestamp"}]}`` stores;
- ``POST /v1/default/banks/{bank}/memories/recall`` with ``{"query", "max_tokens"}``
  returns ``{"results": [{"text", ...}]}``.

Every failure is logged and ignored: the vault keeps working without it.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

import httpx
from loguru import logger

_TIMEOUT_S = 8.0


class Hindsight:
    def __init__(self, client: httpx.AsyncClient, url: str, bank: str = "jarvis") -> None:
        self._client = client
        self._base = f"{url.rstrip('/')}/v1/default/banks/{quote(bank or 'jarvis', safe='')}"
        self._bank = bank or "jarvis"
        self._ready = False

    async def _ensure(self) -> None:
        if self._ready:
            return
        response = await self._client.put(self._base, json={"name": self._bank}, timeout=_TIMEOUT_S)
        if response.status_code >= 400 and response.status_code != 409:
            response.raise_for_status()
        self._ready = True

    async def retain(self, content: str, context: str, when: datetime | None = None) -> None:
        item = {
            "content": content,
            "context": context,
            "timestamp": (when or datetime.now().astimezone()).isoformat(),
        }
        try:
            await self._ensure()
            response = await self._client.post(
                f"{self._base}/memories", json={"items": [item]}, timeout=_TIMEOUT_S
            )
            response.raise_for_status()
        except (httpx.HTTPError, ValueError) as exc:
            logger.info("Hindsight didn't store the memory: {}", type(exc).__name__)

    async def recall(self, query: str, max_tokens: int = 300) -> list[str]:
        try:
            await self._ensure()
            response = await self._client.post(
                f"{self._base}/memories/recall",
                json={"query": query, "max_tokens": max_tokens},
                timeout=_TIMEOUT_S,
            )
            response.raise_for_status()
            results = response.json().get("results", [])
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            logger.info("Hindsight recall failed: {}", type(exc).__name__)
            return []
        if not isinstance(results, list):
            return []
        return [
            " ".join(str(r.get("text", "")).split())
            for r in results
            if isinstance(r, dict) and str(r.get("text", "")).strip()
        ]
