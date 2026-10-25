"""The Decision and Troubleshooting assistants: extra guidance in the prompt when a
request is a choice or a problem, and a read-only PC check."""

from __future__ import annotations

from typing import Any

import pytest

from jarvis.agent.prompts import chat_prompt, guidance, system_prompt
from jarvis.agent.toolbudget import groups_for
from jarvis.tools import diagnostics
from jarvis.tools.registry import ToolRegistry


@pytest.mark.parametrize(
    ("text", "decision", "trouble"),
    [
        ("Should I buy the MacBook Air or the ThinkPad?", True, False),
        ("help me decide between two job offers", True, False),
        ("pros and cons of moving to Bangalore", True, False),
        ("my laptop is really slow today", False, True),
        ("the wifi keeps dropping", False, True),
        ("Spotify won't open", False, True),
        ("what's the capital of Peru", False, False),
    ],
)
def test_guidance(text: str, decision: bool, trouble: bool) -> None:
    extra = guidance(text)
    assert ("Decision help" in extra) is decision
    assert ("Troubleshooting" in extra) is trouble


def test_guidance_goes_into_both_prompts() -> None:
    extra = guidance("should I take the bus or the train?")
    assert "Decision help" in system_prompt(spoken=False, extra=extra)
    assert "Decision help" in chat_prompt(spoken=False, extra=extra)
    assert "Decision help" not in system_prompt(spoken=False)


def test_troubleshooting_gets_the_system_tools() -> None:
    assert "system" in groups_for("my laptop is slow and keeps freezing")


def test_the_pc_check_reads_and_changes_nothing() -> None:
    report = diagnostics.check("slow", sample_s=0.05)
    assert report["uptime"].endswith("since the last restart")
    assert "cores" in report["cpu"] and "free of" in report["memory"]
    assert isinstance(report["busiest"]["memory"], list)
    assert "disks" in report and "network" not in report


async def test_check_pc_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake(area: str) -> dict[str, Any]:
        seen.append(area)
        return {"cpu": "3% busy"}

    registry = ToolRegistry()
    diagnostics.register_diagnostic_tools(registry, fake)
    spec = registry.get("check_pc")
    assert spec is not None
    result = await registry.run(spec, {"area": "network"})
    assert result.ok and result.data == {"cpu": "3% busy"} and seen == ["network"]
    assert "nothing was changed" in result.summary
