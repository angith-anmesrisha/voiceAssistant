# Hitodama

A voice-first AI companion for Windows 10/11 (64-bit). It lives in a floating island at
the top of the screen, listens when called, talks back, and does things on your PC.

Status: **M1, live voice loop.** Hold a hotkey, talk, and hear Gemini answer.
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

A flame icon appears in the system tray (check the `^` overflow area).

1. Get a free API key at [Google AI Studio](https://aistudio.google.com/apikey).
2. Right-click the tray icon, choose **Set Gemini API key…**, and paste it.
3. **Hold `Ctrl+Alt+Space`**, speak, and let go. The reply plays through your speakers.
   Press the hotkey again while it's talking to interrupt it.

Right-click the icon and choose **Quit Hitodama** to exit. Only one copy can run at a time.

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
