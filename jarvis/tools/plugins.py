"""Plugins: your own tools, as Python files in ``%LOCALAPPDATA%\\JARVIS\\plugins``.

A plugin is one ``.py`` file with a ``register(jarvis)`` function::

    def register(jarvis):
        @jarvis.tool("Flip a coin.", risk="low")
        def flip_coin() -> str:
            import random
            return random.choice(["Heads.", "Tails."])

Parameters come from the function's signature (str, int, float, bool), and a tool
returns a string, or ``(ok, text)``. Tools appear to the model as ``plugin_<name>``
and default to MEDIUM risk (asked first), so a plugin can only opt out of asking by
saying ``risk="low"`` itself.

A plugin is Python running inside JARVIS with your rights, so JARVIS never loads a new
or changed file without asking: the code is shown and CONFIRM must be typed. Approval
is remembered by the file's SHA-256, so any edit asks again. Declined files aren't
asked about again until they change. Their output is screened like web text.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import json
import re
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from jarvis.storage.keep import Keep
from jarvis.tools.registry import Risk, ToolRegistry, ToolResult, ToolSpec

PREFIX = "plugin_"
_NAME = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}
_RISKS = {"low": Risk.LOW, "medium": Risk.MEDIUM, "high": Risk.HIGH}

AskFn = Callable[[str, str], Awaitable[bool]]  # (headline, code) -> allowed


class PluginError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PluginFile:
    path: Path
    sha256: str
    code: str


def discover(folder: Path) -> list[PluginFile]:
    if not folder.is_dir():
        return []
    found = []
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        found.append(
            PluginFile(path, hashlib.sha256(raw).hexdigest(), raw.decode("utf-8", "replace"))
        )
    return found


def _parameters(func: Callable[..., Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    hints = inspect.get_annotations(func, eval_str=True)
    for name, parameter in inspect.signature(func).parameters.items():
        kind = _TYPES.get(hints.get(name, str))
        if kind is None:
            raise PluginError(f"{func.__name__}: parameter {name} must be str, int, float or bool")
        properties[name] = {"type": kind}
        if parameter.default is inspect.Parameter.empty:
            required.append(name)
    return {"type": "object", "properties": properties, "required": required}


class PluginApi:
    """What a plugin's ``register(jarvis)`` gets."""

    def __init__(self, plugin: str, registry: ToolRegistry) -> None:
        self._plugin = plugin
        self._registry = registry
        self.added: list[str] = []

    def tool(
        self, description: str, *, risk: str = "medium", name: str | None = None
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        level = _RISKS.get(risk.lower())
        if level is None:
            raise PluginError("risk must be 'low', 'medium' or 'high'")

        def add(func: Callable[..., Any]) -> Callable[..., Any]:
            base = (name or func.__name__).lower()
            if not _NAME.match(base):
                raise PluginError(f"tool name {base!r} must be lowercase letters and _")
            tool_name = PREFIX + base
            parameters = _parameters(func)

            async def call(**args: Any) -> ToolResult:
                try:
                    if inspect.iscoroutinefunction(func):
                        value = await func(**args)
                    else:
                        value = await asyncio.to_thread(func, **args)
                except Exception as exc:
                    logger.warning("Plugin tool {} failed: {}", tool_name, type(exc).__name__)
                    return ToolResult(False, f"The plugin failed ({type(exc).__name__}: {exc}).")
                if isinstance(value, tuple) and len(value) == 2:
                    return ToolResult(bool(value[0]), str(value[1])[:4000])
                return ToolResult(True, str(value if value is not None else "Done.")[:4000])

            self._registry.register(
                ToolSpec(
                    tool_name,
                    f"[plugin {self._plugin}] {description.strip()[:300]}",
                    parameters,
                    call,
                    level,
                    lambda a: f"Run the plugin tool {base} ({self._plugin})",
                )
            )
            self.added.append(tool_name)
            return func

        return add


def is_plugin_tool(name: str) -> bool:
    return name.startswith(PREFIX)


class PluginLoader:
    def __init__(self, folder: Path, registry: ToolRegistry, keep: Keep, ask: AskFn) -> None:
        self._folder = folder
        self._registry = registry
        self._keep = keep
        self._ask = ask

    async def _remembered(self, key: str) -> dict[str, str]:
        try:
            value = json.loads(await self._keep.get_state(key) or "{}")
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    async def load_all(self) -> dict[str, list[str]]:
        """Load approved plugins, asking about new or changed ones. {file: tools}."""
        self._folder.mkdir(parents=True, exist_ok=True)
        approved = await self._remembered("plugins_approved")
        declined = await self._remembered("plugins_declined")
        loaded: dict[str, list[str]] = {}
        for plugin in discover(self._folder):
            name = plugin.path.name
            if approved.get(name) != plugin.sha256:
                if declined.get(name) == plugin.sha256:
                    continue
                state = "changed" if name in approved else "new"
                allowed = await self._ask(
                    f"Load the {state} plugin {name}? It runs as Python inside JARVIS.",
                    plugin.code[:3000],
                )
                target = approved if allowed else declined
                target[name] = plugin.sha256
                (declined if allowed else approved).pop(name, None)
                await self._keep.set_state("plugins_approved", json.dumps(approved))
                await self._keep.set_state("plugins_declined", json.dumps(declined))
                if not allowed:
                    continue
            try:
                loaded[name] = self._load(plugin)
            except Exception as exc:
                logger.warning("Plugin {} didn't load: {}", name, exc)
        return loaded

    def _load(self, plugin: PluginFile) -> list[str]:
        module_name = "jarvis_plugin_" + re.sub(r"\W", "_", plugin.path.stem)
        spec = importlib.util.spec_from_file_location(module_name, plugin.path)
        if spec is None or spec.loader is None:
            raise PluginError("not a Python file")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        # Run exactly the code that was approved (not whatever is on disk a moment later).
        exec(compile(plugin.code, str(plugin.path), "exec"), module.__dict__)
        register = getattr(module, "register", None)
        if not callable(register):
            raise PluginError("it has no register(jarvis) function")
        api = PluginApi(plugin.path.stem, self._registry)
        register(api)
        logger.info("Plugin {} added {} tool(s)", plugin.path.name, len(api.added))
        return api.added
