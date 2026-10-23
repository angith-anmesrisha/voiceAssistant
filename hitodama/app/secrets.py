"""API key storage in the OS credential store (Windows Credential Manager).

The key is never written to a plain file and never logged.
"""

from __future__ import annotations

import contextlib

import keyring
from keyring.errors import KeyringError, PasswordDeleteError
from loguru import logger

from hitodama import APP_NAME

_SERVICE = APP_NAME
_GEMINI_KEY = "gemini_api_key"


def get_api_key() -> str | None:
    try:
        return keyring.get_password(_SERVICE, _GEMINI_KEY)
    except KeyringError as exc:
        logger.error("Could not read API key from credential store: {}", type(exc).__name__)
        return None


def set_api_key(key: str) -> None:
    key = key.strip()
    if not key:
        raise ValueError("API key must not be empty")
    keyring.set_password(_SERVICE, _GEMINI_KEY, key)


def delete_api_key() -> None:
    with contextlib.suppress(PasswordDeleteError):
        keyring.delete_password(_SERVICE, _GEMINI_KEY)


def has_api_key() -> bool:
    return bool(get_api_key())
