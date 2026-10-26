"""Sending a Telegram message through the user's own bot (Telegram Bot API).

Set up once in the workflow builder (or as environment variables): ``TELEGRAM_BOT_TOKEN``
from @BotFather, and ``TELEGRAM_CHAT_ID``, the chat it writes to (found by messaging the
bot and pressing "Find my chat"). Sending asks first (MEDIUM). The token is part of
every request URL, so neither URLs nor httpx's error text are ever logged or shown.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
from loguru import logger

from jarvis.app.keys import get_key
from jarvis.tools.registry import Risk, ToolRegistry, ToolResult, ToolSpec

TOKEN = "TELEGRAM_BOT_TOKEN"
CHAT = "TELEGRAM_CHAT_ID"
API = "https://api.telegram.org/bot{token}/{method}"
MAX_TEXT = 4096  # Telegram's limit for one message


class TelegramError(RuntimeError):
    pass


async def _call(client: httpx.AsyncClient, token: str, method: str, payload: dict[str, Any]) -> Any:
    try:
        response = await client.post(API.format(token=token, method=method), json=payload)
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:  # never str(exc): it holds the URL
        raise TelegramError(f"Telegram didn't answer ({type(exc).__name__})") from None
    if not isinstance(data, dict) or not data.get("ok"):
        description = str(data.get("description", "") if isinstance(data, dict) else "")
        if response.status_code == 401:
            description = "the bot token isn't valid"
        raise TelegramError(f"Telegram said: {description[:160] or response.status_code}")
    return data.get("result")


async def send_message(client: httpx.AsyncClient, token: str, chat: str, text: str) -> None:
    await _call(client, token, "sendMessage", {"chat_id": chat, "text": text[:MAX_TEXT]})


async def find_chat(client: httpx.AsyncClient, token: str) -> tuple[str, str] | None:
    """(chat id, chat name) of the newest message sent to the bot, or None."""
    updates = await _call(client, token, "getUpdates", {"limit": 20})
    for update in reversed(updates or []):
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        if "id" in chat:
            name = chat.get("title") or chat.get("first_name") or chat.get("username") or ""
            return str(chat["id"]), str(name)
    return None


def register_telegram_tools(
    registry: ToolRegistry,
    client: httpx.AsyncClient,
    key: Callable[[str], str | None] = get_key,
) -> None:
    async def send_telegram(message: str, chat: str = "") -> ToolResult:
        token = (key(TOKEN) or "").strip()
        chat = chat.strip() or (key(CHAT) or "").strip()
        if not token or not chat:
            return ToolResult(
                False, "Telegram isn't set up yet. Connect it in a Telegram node's settings."
            )
        if not message.strip():
            return ToolResult(False, "There's no message to send.")
        try:
            await send_message(client, token, chat, message.strip())
        except TelegramError as exc:
            logger.warning("Telegram send failed")  # no token, chat or text
            return ToolResult(False, f"Couldn't send it: {exc}.")
        return ToolResult(True, "Sent the Telegram message.")

    registry.register(
        ToolSpec(
            "send_telegram",
            "Send a Telegram message from the user's JARVIS bot (to their saved chat "
            "unless a chat id or @channel is given).",
            {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "maxLength": MAX_TEXT},
                    "chat": {"type": "string", "maxLength": 80},
                },
                "required": ["message"],
            },
            send_telegram,
            Risk.MEDIUM,
            lambda a: f"Send on Telegram: “{str(a.get('message', ''))[:120]}”",
            group="ui",
        )
    )
