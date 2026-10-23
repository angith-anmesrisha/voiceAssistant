# Hitodama

A voice-first AI companion for Windows 10/11 (64-bit). It lives in a floating island at
the top of the screen, listens when called, talks back, and does things on your PC.

Status: **M0, project skeleton.** It starts, shows a tray icon, logs to a file, and quits.
The full spec is in [`docs/MASTER_PROMPT.md`](docs/MASTER_PROMPT.md).

## Setup (Windows)

1. Install **Python 3.12 (64-bit)** from python.org. Tick "Add python.exe to PATH".
2. In this folder, open PowerShell and run:

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   pip install -e ".[dev]"
   ```

   If activation is blocked, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

## Run

```powershell
python -m hitodama
```

A flame icon appears in the system tray (check the `^` overflow area). Right-click it and
choose **Quit Hitodama** to exit. Running it a second time while it is open exits
immediately, because only one instance is allowed.

Options: `--log-level DEBUG`, `--exit-after SECONDS` (quits on its own; for smoke tests).

Logs: `%LOCALAPPDATA%\Hitodama\logs\hitodama.log`
Settings: `%LOCALAPPDATA%\Hitodama\settings.json`
API key: Windows Credential Manager, entry `Hitodama` / `gemini_api_key` (never in a file).

## Develop

```powershell
ruff check .
ruff format --check .
mypy
pytest
```

Tests use a temporary data folder and an in-memory keyring, so they never touch your real
settings or Credential Manager.
