"""The Troubleshooting Assistant's eyes: a read-only health check of the PC.

``check_pc`` reports what's useful for "my laptop is slow", "the internet keeps
dropping", "my disk is full" or "an app keeps crashing": uptime, CPU and memory with
the busiest programs, disk space, battery, whether the network, DNS and the internet
answer, and on Windows the recent error events from the System and Application logs.
It changes nothing and runs no commands; the AI turns it into steps for you, and only
steps JARVIS already has tools for (closing an app, Wi-Fi, volume) are ever offered,
each asking first as usual.
"""

from __future__ import annotations

import asyncio
import socket
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import psutil
from loguru import logger

AREAS = ("general", "slow", "network", "storage", "battery", "crashes")


def _gb(value: float) -> str:
    return f"{value / 1024**3:.1f} GB"


def busiest(sample_s: float = 0.5, top: int = 5) -> dict[str, list[str]]:
    """The programs using the most CPU (over ``sample_s``) and memory, by name only."""
    processes = []
    for proc in psutil.process_iter(["name", "memory_info"]):
        try:
            proc.cpu_percent(None)
            processes.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    time.sleep(sample_s)
    cpu: dict[str, float] = {}
    memory: dict[str, int] = {}
    for proc in processes:
        try:
            name = proc.info["name"] or "?"
            cpu[name] = cpu.get(name, 0.0) + proc.cpu_percent(None)
            info = proc.info.get("memory_info")
            memory[name] = memory.get(name, 0) + (info.rss if info else 0)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    cores = psutil.cpu_count() or 1
    top_cpu = sorted(cpu.items(), key=lambda kv: -kv[1])[:top]
    top_memory = sorted(memory.items(), key=lambda kv: -kv[1])[:top]
    return {
        "cpu": [f"{name}: {share / cores:.0f}%" for name, share in top_cpu if share > 0.5],
        "memory": [f"{name}: {_gb(rss)}" for name, rss in top_memory],
    }


def disks() -> list[str]:
    lines = []
    for part in psutil.disk_partitions(all=False):
        if "cdrom" in part.opts or not part.fstype:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        warn = " (low)" if usage.percent >= 90 else ""
        lines.append(
            f"{part.mountpoint} {_gb(usage.free)} free of {_gb(usage.total)}"
            f" ({100 - usage.percent:.0f}% free){warn}"
        )
    return lines


def network(host: str = "www.google.com") -> list[str]:
    lines = []
    up = [
        name
        for name, stats in psutil.net_if_stats().items()
        if stats.isup and not name.lower().startswith(("loopback", "lo"))
    ]
    lines.append(f"Connected adapters: {', '.join(up[:4]) or 'none'}")
    try:
        started = time.monotonic()
        socket.getaddrinfo(host, 443)
        lines.append(f"DNS works ({(time.monotonic() - started) * 1000:.0f} ms)")
    except OSError:
        lines.append("DNS failed: names can't be looked up")
    for label, address in (("the internet (1.1.1.1)", "1.1.1.1"), (host, host)):
        try:
            started = time.monotonic()
            with socket.create_connection((address, 443), timeout=4):
                pass
            lines.append(f"Reached {label} in {(time.monotonic() - started) * 1000:.0f} ms")
        except OSError:
            lines.append(f"Couldn't reach {label}")
    return lines


def battery() -> list[str]:
    status = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    if status is None:
        return ["No battery (desktop) or it can't be read"]
    plugged = "plugged in" if status.power_plugged else "on battery"
    return [f"{status.percent:.0f}%, {plugged}"]


def windows_errors(hours: int = 24, limit: int = 10) -> list[str]:
    """Recent Error/Critical events from the System and Application logs (Windows)."""
    if sys.platform != "win32":
        return []
    try:
        import win32evtlog
    except ImportError:
        return []
    since = datetime.now() - timedelta(hours=hours)
    found: list[str] = []
    for log in ("System", "Application"):
        handle = None
        try:
            handle = win32evtlog.OpenEventLog(None, log)
            flags = win32evtlog.EVENTLOG_BACKWARDS_READ | win32evtlog.EVENTLOG_SEQUENTIAL_READ
            done = False
            while not done and len(found) < limit:
                events = win32evtlog.ReadEventLog(handle, flags, 0)
                if not events:
                    break
                for event in events:
                    when = event.TimeGenerated
                    if when < since:
                        done = True
                        break
                    if event.EventType in (1,):  # EVENTLOG_ERROR_TYPE
                        source = str(event.SourceName)[:60]
                        code = event.EventID & 0xFFFF
                        found.append(f"{when:%d %b %H:%M} {log}: {source} (event {code})")
                        if len(found) >= limit:
                            break
        except Exception as exc:  # access denied, log missing, ...
            logger.info("Event log {} unreadable: {}", log, type(exc).__name__)
        finally:
            if handle is not None:
                win32evtlog.CloseEventLog(handle)
    return found


def check(area: str = "general", *, sample_s: float = 0.5) -> dict[str, Any]:
    area = area if area in AREAS else "general"
    boot = datetime.fromtimestamp(psutil.boot_time())
    uptime = datetime.now() - boot
    memory = psutil.virtual_memory()
    report: dict[str, Any] = {
        "uptime": f"{uptime.days} days {uptime.seconds // 3600} hours since the last restart",
        "cpu": f"{psutil.cpu_percent(interval=sample_s):.0f}% busy, {psutil.cpu_count()} cores",
        "memory": f"{memory.percent:.0f}% used ({_gb(memory.available)} free of "
        f"{_gb(memory.total)})",
    }
    if area in ("general", "slow", "crashes"):
        report["busiest"] = busiest(sample_s)
    if area in ("general", "storage", "slow"):
        report["disks"] = disks()
    if area in ("general", "network"):
        report["network"] = network()
    if area in ("general", "battery"):
        report["battery"] = battery()
    if area in ("general", "crashes", "slow", "network"):
        errors = windows_errors()
        if errors:
            report["recent_errors"] = errors
    return report


def register_diagnostic_tools(registry: Any, run: Callable[[str], dict[str, Any]] = check) -> None:
    from jarvis.tools.registry import Risk, ToolResult, ToolSpec

    async def check_pc(area: str = "general") -> ToolResult:
        try:
            report = await asyncio.to_thread(run, area)
        except Exception as exc:
            return ToolResult(False, f"The check didn't finish ({type(exc).__name__}).")
        return ToolResult(True, f"PC check ({area}) done; nothing was changed.", report)

    registry.register(
        ToolSpec(
            "check_pc",
            "Read-only health check for troubleshooting: uptime, CPU/memory and the busiest "
            "programs, disk space, battery, network/DNS/internet, recent Windows error "
            "events. area: general, slow, network, storage, battery or crashes.",
            {
                "type": "object",
                "properties": {"area": {"type": "string", "enum": list(AREAS)}},
                "required": [],
            },
            check_pc,
            Risk.LOW,
            lambda a: "Check the PC",
            group="system",
        )
    )
