"""SQLite persistence for conversations.

Uses the standard-library ``sqlite3`` on a worker thread (one connection, serialized
by a lock) so the UI thread never blocks on disk.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jarvis.providers.base import Message

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    -- Full OpenAI-format message (tool calls etc.) for replaying context.
    payload TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    mode TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id);
"""


@dataclass(frozen=True, slots=True)
class StoredMessage:
    id: int
    role: str
    content: str
    payload: Message
    provider: str | None
    model: str | None
    mode: str | None
    created_at: str


class HistoryStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    async def _run(self, fn: Any, *args: Any) -> Any:
        def locked() -> Any:
            with self._lock:
                return fn(*args)

        return await asyncio.to_thread(locked)

    # --- sync implementations (run on the worker thread) ---

    def _new_conversation(self, title: str) -> int:
        cursor = self._conn.execute(
            "INSERT INTO conversations (created_at, title) VALUES (?, ?)", (_now(), title)
        )
        self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def _latest_conversation(self) -> int | None:
        row = self._conn.execute("SELECT id FROM conversations ORDER BY id DESC LIMIT 1").fetchone()
        return int(row[0]) if row else None

    def _add(self, conversation_id: int, payload: Message, meta: dict[str, str | None]) -> int:
        content = payload.get("content")
        if not isinstance(content, str):
            content = json.dumps(content) if content is not None else ""
        cursor = self._conn.execute(
            "INSERT INTO messages (conversation_id, created_at, role, content, payload, provider,"
            " model, mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                conversation_id,
                _now(),
                str(payload.get("role", "")),
                content,
                json.dumps(payload),
                meta.get("provider"),
                meta.get("model"),
                meta.get("mode"),
            ),
        )
        self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def _messages(self, conversation_id: int, limit: int) -> list[StoredMessage]:
        rows = self._conn.execute(
            "SELECT id, role, content, payload, provider, model, mode, created_at FROM messages"
            " WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
        return [
            StoredMessage(r[0], r[1], r[2], json.loads(r[3]), r[4], r[5], r[6], r[7])
            for r in reversed(rows)
        ]

    def _messages_after(self, after_id: int, limit: int) -> list[StoredMessage]:
        rows = self._conn.execute(
            "SELECT id, role, content, payload, provider, model, mode, created_at FROM messages"
            " WHERE id > ? ORDER BY id LIMIT ?",
            (after_id, limit),
        ).fetchall()
        return [
            StoredMessage(r[0], r[1], r[2], json.loads(r[3]), r[4], r[5], r[6], r[7]) for r in rows
        ]

    def _clear(self, conversation_id: int) -> None:
        self._conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        self._conn.commit()

    # --- async API ---

    async def new_conversation(self, title: str = "") -> int:
        return int(await self._run(self._new_conversation, title))

    async def latest_or_new_conversation(self) -> int:
        latest = await self._run(self._latest_conversation)
        return int(latest) if latest is not None else await self.new_conversation()

    async def add_message(
        self,
        conversation_id: int,
        payload: Message,
        *,
        provider: str | None = None,
        model: str | None = None,
        mode: str | None = None,
    ) -> int:
        meta = {"provider": provider, "model": model, "mode": mode}
        return int(await self._run(self._add, conversation_id, payload, meta))

    async def messages(self, conversation_id: int, limit: int = 200) -> list[StoredMessage]:
        result: list[StoredMessage] = await self._run(self._messages, conversation_id, limit)
        return result

    async def messages_after(self, after_id: int, limit: int = 200) -> list[StoredMessage]:
        """Messages of any conversation newer than ``after_id``, oldest first."""
        result: list[StoredMessage] = await self._run(self._messages_after, after_id, limit)
        return result

    async def last_message_id(self) -> int:
        def go() -> int:
            row = self._conn.execute("SELECT MAX(id) FROM messages").fetchone()
            return int(row[0] or 0)

        result: int = await self._run(go)
        return result

    async def clear(self, conversation_id: int) -> None:
        await self._run(self._clear, conversation_id)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
