"""Long-term memory for the assistant: the Markdown vault, plus Hindsight if configured.

The assistant calls ``recall`` before each answer and ``log`` after it. Both are safe to
call always: they do nothing when switched off, and failures are logged, never raised.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import date, datetime

from loguru import logger

from jarvis.storage.hindsight import Hindsight
from jarvis.storage.vault import Hit, Vault, recall_block

# Short chit-chat isn't worth a search ("thanks", "ok", "stop").
_MIN_RECALL_WORDS = 3


class Memory:
    def __init__(
        self,
        vault: Vault,
        *,
        hindsight: Hindsight | None = None,
        log_conversations: bool = True,
        auto_recall: bool = True,
        budget: int = 700,
    ) -> None:
        self.vault = vault
        self._hindsight = hindsight
        self._log = log_conversations
        self._auto_recall = auto_recall
        self._budget = budget

    async def recall(self, query: str) -> str:
        """Passages that match the request, for the prompt ("" when none or off)."""
        if not self._auto_recall or len(query.split()) < _MIN_RECALL_WORDS:
            return ""
        try:
            hits = await self.vault.asearch(query, limit=4)
        except Exception as exc:
            logger.warning("Memory search failed: {}", type(exc).__name__)
            hits = []
        if self._hindsight is not None:
            extra = await self._hindsight.recall(query, max_tokens=self._budget // 4)
            hits += [Hit("Hindsight", text, 0.0) for text in extra[:3]]
        return recall_block(hits, self._budget)

    async def search(self, query: str, limit: int = 6) -> list[Hit]:
        return await self.vault.asearch(query, limit=limit)

    async def log(self, asked: str, answered: str) -> None:
        """Keep one exchange in today's note (and in Hindsight)."""
        if not self._log or not asked.strip():
            return
        when = datetime.now().astimezone()
        await self.vault.run(self.vault.log_exchange, asked, answered, when)
        if self._hindsight is not None:
            await self._hindsight.retain(
                f"User: {asked}\nJARVIS: {answered}", "conversation with JARVIS", when
            )

    async def summary(self, day: date, text: str) -> None:
        await self.vault.run(self.vault.add_summary, day, text)
        if self._hindsight is not None:
            await self._hindsight.retain(text, "summary of a conversation with JARVIS")

    async def note(self, title: str, text: str) -> str:
        return await asyncio.to_thread(self.vault.write_note, title, text)

    async def facts_changed(self, facts: Iterable[tuple[str, str, str]]) -> None:
        await self.vault.run(self.vault.write_facts, list(facts))
