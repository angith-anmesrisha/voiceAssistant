"""The code runner: its own folder, no secrets, refused programs, HIGH risk every time."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jarvis.app.config import ToolSettings
from jarvis.tools.apps import AppIndex
from jarvis.tools.builtin import build_registry
from jarvis.tools.code_runner import CodeRefusedError, check, run_python
from jarvis.tools.registry import Risk
from tests.fakes import FakeDesktop
from tests.test_tools import risk, run


@pytest.mark.parametrize(
    "code",
    [
        "import subprocess\nsubprocess.run(['calc'])",
        "import os\nos.system('dir')",
        "from ctypes import windll",
        "import winreg",
        "import shutil\nshutil.rmtree('C:/')",
        "from pathlib import Path\nPath('x').unlink()",
        "exec('print(1)')",
        "__import__('subprocess')",
        "import importlib\nimportlib.import_module('subprocess')",
        "print(",
    ],
)
def test_risky_programs_are_refused(code: str) -> None:
    with pytest.raises(CodeRefusedError):
        check(code)


def test_a_program_runs_in_its_folder_without_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gsk_secret")
    folder = tmp_path / "code"
    result = run_python(
        "import os, pathlib\n"
        "pathlib.Path('out.txt').write_text('42')\n"
        "print(sum(range(10)), os.environ.get('GROQ_API_KEY'), pathlib.Path.cwd().name)",
        folder,
    )
    assert result.exit_code == 0 and result.output.split() == ["45", "None", "code"]
    assert (folder / "out.txt").read_text() == "42"
    assert os.environ["GROQ_API_KEY"] == "gsk_secret"  # untouched in JARVIS itself


def test_long_output_is_cut_and_slow_programs_are_stopped(tmp_path: Path) -> None:
    loud = run_python("print('x' * 10000)", tmp_path)
    assert loud.cut and len(loud.output) == 4000
    slow = run_python("import time\nprint('start', flush=True)\ntime.sleep(5)", tmp_path, 0.5)
    assert slow.exit_code is None


async def test_run_python_tool_is_high_risk_and_reports(tmp_path: Path) -> None:
    registry = build_registry(
        ToolSettings(), FakeDesktop(), AppIndex([], apps_folder=False), code_folder=tmp_path
    )
    spec = registry.get("run_python")
    assert spec is not None
    assert risk(registry, "run_python", code="print(1)") is Risk.HIGH
    shown = spec.describe({"code": "print(2 + 2)", "purpose": "Add numbers"})
    assert shown.startswith("Add numbers. Python program") and "print(2 + 2)" in shown
    ok = await run(registry, "run_python", code="print(2 + 2)")
    assert ok.ok and ok.data["output"].strip() == "4"
    broken = await run(registry, "run_python", code="raise SystemExit(3)")
    assert not broken.ok and "exit code 3" in broken.summary
    refused = await run(registry, "run_python", code="import subprocess")
    assert not refused.ok and "won't run" in refused.summary
    assert (
        build_registry(ToolSettings(), FakeDesktop(), AppIndex([], apps_folder=False)).get(
            "run_python"
        )
        is None
    )
