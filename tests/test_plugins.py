"""Plugins: approved by hash, asked again when changed, declined ones left alone."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.storage.keep import Keep
from jarvis.tools.plugins import PluginApi, PluginError, PluginLoader
from jarvis.tools.registry import Risk, ToolRegistry
from tests.test_tools import risk, run

COIN = """
def register(jarvis):
    @jarvis.tool("Flip a coin.", risk="low")
    def flip_coin() -> str:
        return "Heads."

    @jarvis.tool("Add two numbers.")
    def add(a: int, b: int = 1):
        return (a + b > 0, f"{a + b}")
"""


class Asker:
    def __init__(self, *answers: bool) -> None:
        self.answers = list(answers)
        self.asked: list[tuple[str, str]] = []

    async def __call__(self, headline: str, code: str) -> bool:
        self.asked.append((headline, code))
        return self.answers.pop(0)


async def test_plugins_load_only_after_approval(tmp_path: Path) -> None:
    folder = tmp_path / "plugins"
    folder.mkdir()
    (folder / "coin.py").write_text(COIN)
    keep = Keep(tmp_path / "keep.sqlite3")
    registry = ToolRegistry()
    ask = Asker(True)
    loaded = await PluginLoader(folder, registry, keep, ask).load_all()
    assert loaded == {"coin.py": ["plugin_flip_coin", "plugin_add"]}
    assert ask.asked[0][0].startswith("Load the new plugin coin.py?")
    assert "def flip_coin" in ask.asked[0][1]
    assert risk(registry, "plugin_flip_coin") is Risk.LOW
    assert risk(registry, "plugin_add", a=1) is Risk.MEDIUM  # asks unless it says low
    assert (await run(registry, "plugin_flip_coin")).summary == "Heads."
    added = await run(registry, "plugin_add", a=2)
    assert added.ok and added.summary == "3"

    # Next start: same file, no question.
    again = Asker()
    assert await PluginLoader(folder, ToolRegistry(), keep, again).load_all() == loaded
    assert again.asked == []

    # Edited: asked again; declined: not loaded, and not asked again until it changes.
    (folder / "coin.py").write_text(COIN + "\n# changed\n")
    no = Asker(False)
    assert await PluginLoader(folder, ToolRegistry(), keep, no).load_all() == {}
    assert no.asked[0][0].startswith("Load the changed plugin")
    quiet = Asker()
    assert await PluginLoader(folder, ToolRegistry(), keep, quiet).load_all() == {}
    assert quiet.asked == []


async def test_a_broken_plugin_does_not_stop_the_others(tmp_path: Path) -> None:
    folder = tmp_path / "plugins"
    folder.mkdir()
    (folder / "a_broken.py").write_text("raise RuntimeError('oops')")
    (folder / "b_coin.py").write_text(COIN)
    (folder / "_helper.py").write_text("x = 1")  # ignored
    loaded = await PluginLoader(
        folder, ToolRegistry(), Keep(tmp_path / "k.sqlite3"), Asker(True, True)
    ).load_all()
    assert list(loaded) == ["b_coin.py"]


def test_plugin_tools_are_checked() -> None:
    api = PluginApi("x", ToolRegistry())
    with pytest.raises(PluginError):
        api.tool("Bad.", risk="none")
    with pytest.raises(PluginError):
        api.tool("Bad.")(lambda path: path)  # lambda name isn't valid

    def takes_list(items: list) -> str:  # type: ignore[type-arg]
        return ""

    with pytest.raises(PluginError):
        api.tool("Bad.")(takes_list)
