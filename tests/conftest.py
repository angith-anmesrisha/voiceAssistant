from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        super().__init__()  # type: ignore[no-untyped-call]
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        from keyring.errors import PasswordDeleteError

        if (service, username) not in self._store:
            raise PasswordDeleteError(username)
        del self._store[(service, username)]


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point app data at a temp dir and use an in-memory keyring for every test."""
    data = tmp_path / "data"
    monkeypatch.setenv("HITODAMA_DATA_DIR", str(data))
    previous = keyring.get_keyring()
    keyring.set_keyring(MemoryKeyring())
    yield data
    keyring.set_keyring(previous)
