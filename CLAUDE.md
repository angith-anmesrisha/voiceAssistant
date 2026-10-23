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
- `ui/tray.py`, `ui/icons.py`: tray icon (drawn in code), menu, "Set Gemini API key…"
  dialog, and tray balloons for `UserNotice` events.
- `core/hotkeys.py`: `ChordTracker` (pure, tested) + `GlobalHotkeys` (pynput hook) publishing
  `PushToTalkPressed/Released`.
- `core/audio_io.py`: `Microphone` (16 kHz, 32 ms blocks, open only while held) and
  `Speaker` (24 kHz, `PlaybackBuffer`, `on_drained` callback). sounddevice imported lazily.
- `core/live_session.py`: `LiveSession` (Gemini Live connection, queued outgoing audio,
  session resumption, context compression, GoAway resume, backoff reconnect) and
  `classify_error` (invalid key / quota / region / bad config / transient).
- `core/voice_loop.py`: `VoiceLoop` owns the voice turn state machine, barge-in, reply
  timeout, idle disconnect, and friendly notices.
- `core/personality.py`: compact system prompt (identity, personality preset, rules, time).
- `tools/`, `features/`, `ui/island_panels/`, `storage/`: empty packages for later milestones.

## Decisions
- Non-secret settings in JSON for now; may move to SQLite with the M8 storage work.
- `--exit-after` exists so tests can smoke-run the real app headless.
- M1 (verified 2026-10-23 against ai.google.dev and google-genai 2.33): Live model
  `gemini-3.8-live`, text/vision `gemini-3.8-flash`. Input `audio/pcm;rate=16000`, output 24 kHz.
- Push-to-talk uses **manual activity detection** (server VAD disabled; we send
  `activity_start`/`activity_end`). Nothing is sent unless the key is held.
- The Live connection opens on the first press (audio is queued meanwhile) and closes after
  `live.idle_disconnect_s` (300 s) idle. The resumption handle is kept, so context carries
  over for about 2 hours.
- Barge-in: pressing while it speaks clears playback; audio that arrives while the key is
  held is dropped as part of the talked-over reply.
- The audio hot path uses direct `LiveHandler` callbacks, not the event bus; state, notices
  and captions go through the bus.
- If setup is refused (1007/1008/4xx) before the first connection, `LiveSession` tries
  `models.live_fallbacks` in order (default `gemini-3.1-flash-live-preview`). The server's
  close reason is logged and shown in the error balloon.
- API key entry is a tray dialog for now; the full onboarding flow is M12.
- Transcripts are published as `Transcript` events for captions (M4) and never logged.

## Known issues
- Tray icon is a placeholder drawn in code; a real icon comes with M13 packaging.
- "Settings…" tray item is disabled until the settings window exists.
- Live API calls cannot be exercised in CI (no key). Tests use fakes in `tests/fakes.py`.
- The first question after startup or an idle disconnect waits for the connection (about 1 s extra).
- The Space keystroke of Ctrl+Alt+Space still reaches the focused app (not suppressed).
- Errors are shown as tray balloons only. Spoken errors and the island come in M4.

## Current milestone
**M1 (live voice loop)**: built, awaiting owner confirmation on Windows. Next: M2, tools and
function calling. (M0 confirmed 2026-10-23.)
