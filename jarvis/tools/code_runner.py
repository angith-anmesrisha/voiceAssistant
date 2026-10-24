"""Running a short Python program JARVIS wrote, in its own folder, after you confirm.

The owner asked for this (2026-10-24, "each asks first"), so it is HIGH risk: the full
program is shown and CONFIRM must be typed before every run. Even then:

- only Python, run in isolated mode (``-I``: no user site-packages, no env tweaks), with
  its own folder (``%LOCALAPPDATA%\\JARVIS\\code``) as the working directory;
- no API keys or other secrets in its environment, no console window, no input;
- 30 seconds at most, output cut to 4,000 characters;
- programs that start other programs, delete files or reach into Windows (subprocess,
  os.system, os.remove, ctypes, winreg, the pywin32 modules, exec/eval tricks...) are
  refused before running.

This is a guard rail, not a sandbox: an approved program runs as you. The confirmation,
with the code on screen, is the real control.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarvis.tools.registry import Risk, ToolRegistry, ToolResult, ToolSpec

TIMEOUT_S = 30.0
MAX_OUTPUT = 4000

_BLOCKED_MODULES = frozenset(
    {
        "subprocess", "ctypes", "_ctypes", "winreg", "_winreg", "_winapi", "msvcrt",
        "multiprocessing", "pty", "importlib", "runpy", "code", "pythoncom", "pywintypes",
        "win32api", "win32con", "win32com", "win32process", "win32service", "win32security",
        "comtypes", "pywinauto", "uiautomation", "pynput", "pyautogui", "keyboard",
    }
)  # fmt: skip
_BLOCKED_CALLS = frozenset(
    {
        "system", "popen", "startfile", "kill", "killpg", "execv", "execve", "execl",
        "execle", "execlp", "execlpe", "execvp", "execvpe", "spawnl", "spawnle", "spawnlp",
        "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe", "posix_spawn", "rmtree",
        "fork", "forkpty", "remove", "unlink", "rmdir", "removedirs",
    }
)  # fmt: skip
_BLOCKED_BUILTINS = frozenset({"exec", "eval", "compile", "__import__", "breakpoint"})


class CodeRefusedError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RunResult:
    exit_code: int | None  # None: stopped at the time limit
    output: str
    cut: bool


def check(code: str) -> None:
    """Refuse programs that would start other programs or reach into Windows."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise CodeRefusedError(f"it isn't valid Python (line {exc.lineno}: {exc.msg})") from exc
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            if name.split(".")[0] in _BLOCKED_MODULES:
                raise CodeRefusedError(f"it uses {name}, which JARVIS's programs may not")
        if isinstance(node, ast.Attribute) and node.attr in _BLOCKED_CALLS:
            raise CodeRefusedError(f"it calls {node.attr}(), which JARVIS's programs may not")
        if isinstance(node, ast.Name) and node.id in _BLOCKED_BUILTINS:
            raise CodeRefusedError(f"it uses {node.id}(), which JARVIS's programs may not")


def console_python(executable: str = sys.executable) -> str:
    """python.exe rather than pythonw.exe (JARVIS may run windowless)."""
    path = Path(executable)
    if path.name.lower() == "pythonw.exe" and path.with_name("python.exe").exists():
        return str(path.with_name("python.exe"))
    return str(path)


def clean_environment(folder: Path) -> dict[str, str]:
    """Just enough to run Python: no keys, tokens or the user's other variables."""
    temp = folder / "tmp"
    temp.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": str(Path(console_python()).parent),
        "TEMP": str(temp),
        "TMP": str(temp),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for name in ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def run_python(code: str, folder: Path, timeout_s: float = TIMEOUT_S) -> RunResult:
    """Check, save as ``program.py`` in ``folder`` and run it. Blocking."""
    check(code)
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / "program.py"
    script.write_text(code, encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        done = subprocess.run(
            [console_python(), "-I", "-X", "utf8", str(script)],
            cwd=folder,
            env=clean_environment(folder),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_s,
            shell=False,
            creationflags=flags,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.output.decode("utf-8", "replace") if isinstance(exc.output, bytes) else ""
        return RunResult(None, output[-MAX_OUTPUT:], len(output) > MAX_OUTPUT)
    output = done.stdout.decode("utf-8", "replace")
    cut = len(output) > MAX_OUTPUT
    return RunResult(done.returncode, output[:MAX_OUTPUT] if cut else output, cut)


def register_code_tool(registry: ToolRegistry, folder: Path) -> None:
    async def run_program(code: str, purpose: str = "") -> ToolResult:
        import asyncio

        try:
            result = await asyncio.to_thread(run_python, code, folder)
        except CodeRefusedError as exc:
            return ToolResult(False, f"JARVIS won't run that program: {exc}.")
        except OSError as exc:
            return ToolResult(False, f"The program couldn't start ({exc.strerror or exc}).")
        data: dict[str, Any] = {"output": result.output or "(no output)"}
        if result.exit_code is None:
            return ToolResult(False, f"Stopped the program after {TIMEOUT_S:.0f} seconds.", data)
        note = " (output shortened)" if result.cut else ""
        if result.exit_code != 0:
            return ToolResult(
                False, f"The program failed (exit code {result.exit_code}){note}.", data
            )
        return ToolResult(True, f"The program ran{note}.", data)

    def describe(args: dict[str, Any]) -> str:
        code = str(args.get("code", ""))
        shown = code if len(code) <= 1500 else code[:1500] + "\n…"
        purpose = str(args.get("purpose") or "Run a Python program").strip().rstrip(".")
        return f"{purpose}. Python program, run in {folder}:\n{shown}"

    registry.register(
        ToolSpec(
            "run_python",
            "Write and run a short Python program (standard library only) for calculations, "
            "data or text processing, or making a file. It runs in JARVIS's own code folder "
            "(relative paths land there), up to 30 s, and can't start other programs or "
            "delete files. The user must approve every run, so explain the purpose.",
            {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "maxLength": 20_000},
                    "purpose": {"type": "string", "maxLength": 200},
                },
                "required": ["code"],
            },
            run_program,
            Risk.HIGH,
            describe,
            group="code",
        )
    )
