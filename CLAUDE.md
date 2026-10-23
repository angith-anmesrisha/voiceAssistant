# CLAUDE.md: Hitodama

Voice-first AI companion for Windows 10/11. Full spec: `docs/MASTER_PROMPT.md`
(written for the placeholder name "Wisp"; the product name is **Hitodama**, package `hitodama`).

## Working rules (from the spec)
- One milestone at a time. Plan first, build, run ruff/mypy/pytest, give a manual test
  checklist, then STOP until the owner confirms.
- Commit after each milestone: `feat(mN): ...`. Never commit secrets.
- Verify Gemini model names and SDK signatures against ai.google.dev and the installed
  `google-genai` source before use. Model names live in config only.
- Every PC action is a `@tool(risk=...)` in `hitodama/tools/`. Modules talk through the
  event bus. Never log audio, API keys, or dictated text.

## Stack
Python 3.12, PySide6 + qasync, google-genai (Live API), sounddevice, pywin32, loguru,
pydantic, keyring, SQLite (aiosqlite), APScheduler, PyInstaller + Inno Setup.

## Commands
- Run: `python -m hitodama` (`--log-level DEBUG`, `--exit-after N`)
- Lint/type/test: `ruff check . && ruff format --check . && mypy && pytest`
- Headless (Linux/CI): set `QT_QPA_PLATFORM=offscreen`.

## Layout (implemented so far)
- `hitodama/main.py`: entry; single-instance lock, QApplication, qasync loop, tray, waits
  for `QuitRequested`.
- `app/paths.py`: data dir (`%LOCALAPPDATA%\Hitodama`; `HITODAMA_DATA_DIR` overrides).
- `app/config.py`: pydantic `Settings` (models, hotkeys, audio, loadout) in `settings.json`.
- `app/secrets.py`: Gemini key in the OS credential store via `keyring`.
- `app/events.py`: `EventBus` (async pub/sub by event class, MRO-aware, isolates handler errors).
- `app/state.py`: `StateMachine` with `ALLOWED_TRANSITIONS`, publishes `StateChanged`.
- `app/single_instance.py`: Windows named mutex; `QLockFile` elsewhere.
- `app/logging_setup.py`: loguru, 5 MB x 5 rotating file, `diagnose=False`.
- `ui/tray.py`, `ui/icons.py`: tray icon (drawn in code) and menu.
- `core/`, `tools/`, `features/`, `ui/island_panels/`, `storage/`: empty packages for later milestones.

## Decisions
- Non-secret settings in JSON for now; may move to SQLite with the M8 storage work.
- Model names in `ModelSettings` are placeholders; M1 must verify them against current docs.
- `--exit-after` exists so tests can smoke-run the real app headless.

## Known issues
- Tray icon is a placeholder drawn in code; a real icon comes with M13 packaging.
- "Settings…" tray item is disabled until the settings window exists.

## Current milestone
**M0 (project skeleton)**: built, awaiting owner confirmation on Windows. Next: M1, live voice loop.
