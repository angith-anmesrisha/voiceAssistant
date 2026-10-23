"""Single-instance lock.

On Windows a named mutex is used (released automatically by the OS if the process
dies). Elsewhere, a Qt lock file in the data dir is used so dev runs behave the same.
"""

from __future__ import annotations

import sys
from types import TracebackType
from typing import Any

from PySide6.QtCore import QLockFile

from hitodama import APP_NAME
from hitodama.app.paths import data_dir


class SingleInstance:
    def __init__(self, name: str = APP_NAME) -> None:
        self._name = name
        self._mutex: Any = None
        self._lock: QLockFile | None = None

    def acquire(self) -> bool:
        """Return True if this is the only running instance."""
        if sys.platform == "win32":
            import win32api
            import win32event
            import winerror

            self._mutex = win32event.CreateMutex(None, False, f"Local\\{self._name}-instance")
            if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
                win32api.CloseHandle(self._mutex)
                self._mutex = None
                return False
            return True
        self._lock = QLockFile(str(data_dir() / f"{self._name.lower()}.lock"))
        self._lock.setStaleLockTime(0)
        return self._lock.tryLock(0)

    def release(self) -> None:
        if self._mutex is not None:
            import win32api

            win32api.CloseHandle(self._mutex)
            self._mutex = None
        if self._lock is not None:
            self._lock.unlock()
            self._lock = None

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
