"""Lightweight internet reachability check (a TCP connect, no HTTP request)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass

_TARGETS = (("api.groq.com", 443), ("generativelanguage.googleapis.com", 443), ("1.1.1.1", 443))


@dataclass(frozen=True, slots=True)
class NetworkStatus:
    online: bool
    latency_ms: int | None = None


async def check_internet(timeout_s: float = 3.0) -> NetworkStatus:
    for host, port in _TARGETS:
        started = time.perf_counter()
        try:
            _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout_s)
        except (OSError, TimeoutError):
            continue
        latency = int((time.perf_counter() - started) * 1000)
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        return NetworkStatus(True, latency)
    return NetworkStatus(False)
