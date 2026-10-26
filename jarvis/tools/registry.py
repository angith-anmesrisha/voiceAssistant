"""Tool registry: declarations for the model, risk levels, argument checks."""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class Risk(IntEnum):
    """LOW runs directly; MEDIUM asks first; HIGH needs a strong, typed confirmation."""

    LOW = 1
    MEDIUM = 2
    HIGH = 3


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)

    def as_payload(self) -> dict[str, Any]:
        return {"ok": self.ok, "summary": self.summary, **self.data}


# Tools in this group are never offered to the model, and the assistant refuses them
# unless a workflow step runs them (the login fill-in).
WORKFLOW_ONLY = "workflow_only"

ToolFunc = Callable[..., ToolResult | Awaitable[ToolResult]]
RiskRule = Risk | Callable[[dict[str, Any]], Risk]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    func: ToolFunc
    risk: RiskRule
    # Human-readable sentence shown in confirmations, e.g. "Type 42 characters".
    describe: Callable[[dict[str, Any]], str]
    # Which requests get this tool (see jarvis/agent/toolbudget.py); "core" always does.
    group: str = "core"

    def risk_for(self, args: dict[str, Any]) -> Risk:
        return self.risk(args) if callable(self.risk) else self.risk

    def declaration(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ArgumentError(ValueError):
    pass


_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
}


def validate_args(schema: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """Check model-supplied arguments against a flat JSON schema; drop unknown keys."""
    properties: dict[str, Any] = schema.get("properties", {})
    clean: dict[str, Any] = {}
    for name in schema.get("required", []):
        if name not in args:
            raise ArgumentError(f"missing argument '{name}'")
    for name, value in args.items():
        spec = properties.get(name)
        if spec is None:
            continue
        expected = _JSON_TYPES.get(spec.get("type", "string"), (str,))
        if isinstance(value, bool) and bool not in expected:
            raise ArgumentError(f"argument '{name}' has the wrong type")
        if not isinstance(value, expected):
            raise ArgumentError(f"argument '{name}' should be {spec.get('type')}")
        if "enum" in spec and value not in spec["enum"]:
            # Models often vary case ("Notepad"); accept that, nothing else.
            matches = [
                e for e in spec["enum"] if isinstance(value, str) and e.lower() == value.lower()
            ]
            if not matches:
                raise ArgumentError(f"argument '{name}' must be one of {spec['enum']}")
            value = matches[0]
        if isinstance(value, str) and "maxLength" in spec and len(value) > spec["maxLength"]:
            raise ArgumentError(f"argument '{name}' is too long")
        clean[name] = value
    return clean


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool {spec.name}")
        self._tools[spec.name] = spec

    def set_group(self, group: str, *names: str) -> None:
        for name in names:
            self._tools[name] = dataclasses.replace(self._tools[name], group=group)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def declarations(self, groups: set[str] | None = None) -> list[dict[str, Any]]:
        """All tools, or only those in ``groups`` (plus "core") to keep requests small."""
        return [
            spec.declaration()
            for spec in self._tools.values()
            if spec.group != WORKFLOW_ONLY
            and (groups is None or spec.group == "core" or spec.group in groups)
        ]

    def groups(self) -> set[str]:
        return {spec.group for spec in self._tools.values()}

    async def run(self, spec: ToolSpec, args: dict[str, Any]) -> ToolResult:
        result = spec.func(**args)
        if inspect.isawaitable(result):
            result = await result
        assert isinstance(result, ToolResult)
        return result
