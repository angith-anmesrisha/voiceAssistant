"""Things JARVIS does on its own: delivering reminders, plus the jobs in
:mod:`jarvis.agent.routines` (morning briefing, watched news topics, hardware alerts,
check-ins, session summaries).

Each job publishes an :class:`Announce`; the controller says it out loud once JARVIS
isn't busy, and the window shows it.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Sequence
from datetime import datetime

from loguru import logger

from jarvis.agent.routines import Job
from jarvis.app.events import Announce, ChatEntry, EntryKind, EventBus
from jarvis.storage.keep import Keep

_LATE_S = 120  # a reminder this late was missed while JARVIS wasn't running


class Background:
    def __init__(
        self,
        bus: EventBus,
        keep: Keep,
        *,
        clock: Callable[[], float] = time.time,
        jobs: Sequence[Job] = (),
    ) -> None:
        self._bus = bus
        self._keep = keep
        self._clock = clock
        self._jobs = list(jobs)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:  # one failing job must not stop the others
                logger.warning("Background job failed: {}", type(exc).__name__)
            await asyncio.sleep(10)

    async def tick(self) -> None:
        await self.deliver_reminders()
        now = datetime.fromtimestamp(self._clock()).astimezone()
        for job in self._jobs:
            try:
                for said in await job.tick(now):
                    await self.announce(said.text, said.kind)
            except Exception as exc:  # one failing job must not stop the others
                logger.warning("{} failed: {}", type(job).__name__, type(exc).__name__)

    async def deliver_reminders(self) -> int:
        now = self._clock()
        delivered = 0
        for reminder in await self._keep.reminders():
            if reminder.due > now:
                break
            if not await self._keep.finish_reminder(reminder.id):
                continue
            text = f"Reminder: {reminder.text}"
            if now - reminder.due > _LATE_S:
                missed = datetime.fromtimestamp(reminder.due).astimezone().strftime("%H:%M")
                text += f" (it was due at {missed}, while I wasn't running)"
            await self.announce(text, "reminder")
            delivered += 1
        return delivered

    async def announce(self, text: str, kind: str) -> None:
        await self._bus.publish(ChatEntry(EntryKind.ASSISTANT, text, mode=kind))
        await self._bus.publish(Announce(text, kind))
