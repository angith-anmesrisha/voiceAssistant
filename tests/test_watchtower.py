"""Watchtower: folders you chose are listed (never opened) and new or changed files are
mentioned once they've settled; folder workflows start with {file}; deadlines and
calendar events get one heads-up each; only your own folders can be watched."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from jarvis.agent.watchtower import (
    Change,
    FolderWatcher,
    Watch,
    Watchtower,
    describe_change,
    register_watch_tools,
)
from jarvis.agent.workflows import Step, Trigger, Workflow, WorkflowStore, fill, folder_watches
from jarvis.storage.keep import Keep
from jarvis.tools.calendar_ics import Event
from jarvis.tools.files import FileArea
from jarvis.tools.registry import ToolRegistry

NOW = datetime(2026, 10, 26, 12, 0).astimezone()


@pytest.fixture
def keep(tmp_path: Path) -> Keep:
    return Keep(tmp_path / "keep.sqlite3")


def touch(path: Path, text: str = "x", when: float | None = None) -> None:
    path.write_text(text)
    if when is not None:
        os.utime(path, (when, when))


def test_new_files_are_reported_once_they_settle(tmp_path: Path) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    touch(downloads / "old.pdf")
    watch = Watch(str(downloads), "*.pdf", ["new"])
    watcher = FolderWatcher()
    assert watcher.scan([watch]) == []  # first look: learns what's there
    touch(downloads / "report.pdf", "part")
    touch(downloads / "movie.mp4")  # not a PDF
    touch(downloads / "big.pdf.crdownload")  # still downloading
    assert watcher.scan([watch]) == []  # just appeared: wait one more look
    touch(downloads / "report.pdf", "part and the rest")  # still growing
    assert watcher.scan([watch]) == []
    [change] = watcher.scan([watch])
    assert change.path.name == "report.pdf" and change.event == "new"
    assert describe_change(change) == "New PDF in Downloads: report.pdf."
    assert watcher.scan([watch]) == []  # said once


def test_changed_files_when_asked_for(tmp_path: Path) -> None:
    folder = tmp_path / "Project"
    folder.mkdir()
    touch(folder / "plan.docx", when=1000)
    both = Watch(str(folder), "*", ["new", "changed"])
    only_new = Watch(str(folder), "*", ["new"], id="other")
    watcher = FolderWatcher()
    watcher.scan([both, only_new])
    touch(folder / "plan.docx", "edited", when=2000)
    watcher.scan([both, only_new])
    changes = watcher.scan([both, only_new])
    assert [(c.watch.id, c.event) for c in changes] == [(both.id, "changed")]
    assert describe_change(changes[0]) == "plan.docx in Project was changed."


def test_subfolders_only_when_asked(tmp_path: Path) -> None:
    (tmp_path / "A" / "deep").mkdir(parents=True)
    flat, deep = Watch(str(tmp_path / "A")), Watch(str(tmp_path / "A"), recursive=True, id="d")
    watcher = FolderWatcher()
    watcher.scan([flat, deep])
    touch(tmp_path / "A" / "deep" / "x.txt")
    watcher.scan([flat, deep])
    assert [c.watch.id for c in watcher.scan([flat, deep])] == ["d"]


async def test_watches_are_your_folders_only(tmp_path: Path, keep: Keep) -> None:
    mine = tmp_path / "home"
    (mine / "Downloads").mkdir(parents=True)
    tower = Watchtower(keep)
    registry = ToolRegistry()
    register_watch_tools(registry, tower, FileArea([mine]))

    async def call(name: str, **args: Any) -> Any:
        spec = registry.get(name)
        assert spec is not None
        return await registry.run(spec, args)

    added = await call("watch_folder", folder=str(mine / "Downloads"), pattern="*.pdf")
    assert added.ok and "new *.pdf files in" in added.summary
    outside = await call("watch_folder", folder="/etc")
    assert not outside.ok and "outside your folders" in outside.summary
    listed = await call("list_watches")
    assert len(listed.data["watching"]) == 1
    stopped = await call("stop_watching_folder", folder="downloads")
    assert stopped.ok and (await call("list_watches")).summary == "No folders are being watched."


async def test_the_tower_announces_and_starts_folder_workflows(tmp_path: Path, keep: Keep) -> None:
    inbox = tmp_path / "Scans"
    inbox.mkdir()
    store = WorkflowStore(tmp_path / "w.json")
    workflow = Workflow(
        "File scans",
        Trigger("folder", folder=str(inbox), pattern="*.pdf", event="new"),
        [Step("say", {"text": "Got {file}"})],
    )
    store.put(workflow)
    started: list[Change] = []

    async def on_change(change: Change) -> None:
        started.append(change)

    area = FileArea([tmp_path])
    tower = Watchtower(
        keep,
        interval_s=10,
        deadlines=False,
        on_change=on_change,
        extra=lambda: folder_watches(store, area.resolve),
    )
    mine = await tower.add(area, str(inbox), "*")
    assert await tower.tick(NOW) == []
    touch(inbox / "invoice.pdf")
    await tower.tick(NOW + timedelta(seconds=10))
    said = await tower.tick(NOW + timedelta(seconds=20))
    assert [s.text for s in said] == ["New PDF in Scans: invoice.pdf."]  # the user's watch
    # on_change hears every watch; main starts workflows only for the "wf-" ones.
    assert {c.watch.id for c in started} == {f"wf-{workflow.id}", mine.id}
    assert fill("Got {file}", [], {"file": str(inbox / "invoice.pdf")}).endswith("invoice.pdf")


async def test_deadlines_get_one_heads_up_per_stage(keep: Keep) -> None:
    tomorrow = (NOW + timedelta(hours=20)).timestamp()
    task = await keep.add_task("Submit the lab report", due=tomorrow)
    tower = Watchtower(keep, quiet_hours=(0, 0))
    first = await tower.tick(NOW)
    assert [s.text for s in first] == [
        "Heads up: “Submit the lab report” is due tomorrow at 08:00."
    ]
    assert await tower.tick(NOW + timedelta(minutes=1)) == []
    hour = await tower.tick(NOW + timedelta(hours=19, minutes=30))
    assert hour[0].text == "“Submit the lab report” is due within the hour at 08:00."
    late = await tower.tick(NOW + timedelta(hours=21))
    assert late[0].text == "“Submit the lab report” is now overdue." and late[0].kind == "deadline"
    assert await tower.tick(NOW + timedelta(hours=22)) == []
    await keep.finish_task(task.id)
    assert await tower.tick(NOW + timedelta(hours=23)) == []


async def test_calendar_heads_up(keep: Keep) -> None:
    class Soon:
        available = True

        async def events(self, start: datetime, end: datetime) -> list[Event]:
            begin = NOW + timedelta(minutes=10)
            return [Event("Team sync", begin, begin + timedelta(minutes=30))]

    tower = Watchtower(keep, deadlines=False, calendar=Soon())  # type: ignore[arg-type]
    assert [s.text for s in await tower.tick(NOW)] == ["Team sync starts in 10 minutes."]
    assert await tower.tick(NOW + timedelta(minutes=2)) == []
    tower.calendar_alerts = False
    assert await tower.tick(NOW + timedelta(minutes=3)) == []


async def test_the_watchtower_tab(tmp_path: Path, keep: Keep) -> None:
    app = QApplication.instance() or QApplication([])
    assert app is not None
    from jarvis.ui.watchtower import AutomationWindow, WatchtowerPanel
    from jarvis.ui.workflows import WorkflowBuilder

    (tmp_path / "Downloads").mkdir()
    area = FileArea([tmp_path])
    tower = Watchtower(keep)
    options: list[tuple[str, bool]] = []

    async def add(folder: str, pattern: str, events: list[str], deep: bool) -> str | None:
        try:
            await tower.add(area, folder, pattern, events, deep)
        except Exception as exc:
            return str(exc)
        return None

    async def remove(watch_id: str) -> None:
        await tower.remove(None, watch_id)

    panel = WatchtowerPanel(
        tower.watches, add, remove, on_option=lambda k, on: options.append((k, on))
    )
    window = AutomationWindow(WorkflowBuilder(WorkflowStore(tmp_path / "w.json"), print), panel)
    panel.folder.setText(str(tmp_path / "Downloads"))
    panel.pattern.setText("*.pdf")
    await panel.add()
    assert panel.list.count() == 1 and "*.pdf files" in panel.list.item(0).text()
    panel.folder.setText("/etc")
    await panel.add()
    assert "outside your folders" in panel.message.text()
    panel.list.setCurrentRow(0)
    await panel.remove_selected()
    assert panel.list.count() == 0
    panel.options["deadline_alerts"].setChecked(False)
    assert options == [("deadline_alerts", False)]
    assert window.tabs.count() == 2
