# CLAUDE.md: JARVIS

Voice assistant and agentic AI for the Windows 10/11 desktop. Current requirements:
`docs/JARVIS_SPEC.md`. `docs/MASTER_PROMPT.md` is the older "Wisp/Hitodama" spec; its
working rules and safety rules still apply where they don't conflict.

## Working rules
- Plan first, build, run ruff/mypy/pytest, give a manual test checklist, then wait for the
  owner to confirm on Windows.
- Work on a branch and open a PR to `main`. Never commit secrets.
- Verify model names and API shapes against provider docs before use. Model names live in
  config only.
- Every PC action is a tool in `jarvis/tools/builtin.py` with a `Risk`. There is no shell,
  file, or registry tool, and none may be added. Never log audio, API keys, or dictated text.
- Never claim a provider/model answered unless it did; the UI shows the real provider/model.

## Stack
Python 3.12+ (tested on 3.12, 3.13, 3.14), PySide6 + qasync, httpx (OpenAI-compatible endpoints), sounddevice, pynput,
pywin32 (SAPI TTS), psutil, loguru, pydantic, stdlib sqlite3.

## Commands
- Run: `python -m jarvis` (`--log-level DEBUG`, `--hidden`, `--exit-after N`)
- Lint/type/test: `ruff check . && ruff format --check . && mypy && pytest`
- Headless (Linux/CI): set `QT_QPA_PLATFORM=offscreen`.
- Renders: `QT_QPA_PLATFORM=offscreen PYTHONPATH=. python scripts/render_ui.py docs/renders`

## Layout
- `jarvis/main.py`: entry; wires router, store, tools, window, assistant, voice, tray, hotkeys.
- `app/`: paths (`%LOCALAPPDATA%\JARVIS`, `JARVIS_DATA_DIR` override), config (pydantic
  `Settings` in `settings.json`), events (`EventBus`), state (`StateMachine`), logging,
  single instance.
- `providers/`: `base.py` (types, `ProviderError`, `ErrorKind`, `ProviderState`),
  `openai_compat.py` (shared chat/transcribe/probe), `backends.py` (Groq, Gemini, Ollama),
  `network.py` (TCP reachability), `router.py` (priority, capability routing, bounded
  retries with backoff, fallback, `AllProvidersFailedError`).
- `app/keys.py`: API keys from the process env, then (Windows) the user env in
  `HKCU\Environment`; saving from the UI writes there too, like `setx`.
- `storage/db.py`: `HistoryStore` (SQLite conversations + messages, via `asyncio.to_thread`).
- `tools/`: `registry.py` (`Risk`, `ToolSpec`, arg validation), `desktop.py` (`WindowsDesktop`:
  approved launches with `shell=False`, pynput keys/typing, lock/sleep, delayed shutdown),
  `builtin.py` (the tool set).
- `agent/`: `commands.py` ("use Gemini from now on" → provider switch, no model call),
  `prompts.py` (one system prompt; `wants_screen` regex), `assistant.py` (one turn:
  history, screen fast path, otherwise a tool loop with tools + `look_at_screen`,
  confirmations).
- `voice/`: `audio_io.py` (mic → WAV recorder, `EndOfSpeech` for hands-free), `tts.py`
  (SAPI on its own thread), `hotkeys.py` (global push-to-talk; Windows hook swallows the
  chord's main key), `local_stt.py` (offline SAPI dictation fallback), `wake_word.py`
  (offline SAPI command grammar for "Jarvis").
- `controller.py`: glues UI, voice, and assistant; owns busy/listening.
- `ui/`: `theme.py`, `main_window.py` (HUD stage, tweens, layout), `tray.py`, `icons.py`,
  `widgets/`: `orb.py` (particle globe when idle, morphs into the glowing ring), `stage.py`
  (backdrop, top bar, clock/state caption, reply card, status strip, `Tween`), `panels.py`
  (provider/network/safety panels in the details overlay, command bar, confirm overlay),
  `chat.py` (history drawer), `keys.py` (API key overlay), `glass.py`, `overlay.py`
  (always-on-top corner orb). `frame_clock.py`: one shared animation clock (vsync via
  `DwmFlush` on Windows, precise timer elsewhere).
- `assets/fonts/`: Orbitron, Rajdhani, Inter (OFL).

## Decisions
- All three providers use their OpenAI-compatible chat-completions endpoints, so history and
  tool calls move between providers unchanged. Groq STT uses `/audio/transcriptions`; Gemini
  STT goes through chat with an `input_audio` part; Ollama has no STT.
- Keys come only from environment variables (`GROQ_API_KEY`, `GEMINI_API_KEY`). The key
  button in AI PROVIDERS saves them to the user environment (same as `setx`).
- Ollama: chat and vision model `qwen3.5` (= `qwen3.5:latest`) with `reasoning_effort:
  "none"` so it doesn't think; if the vision model is missing, any installed model whose
  `/api/show` capabilities include `vision`.
- No modes. Screen questions (regex `wants_screen`) go straight to vision; everything else
  goes to the agent with all tools plus `look_at_screen`, so the model decides. The reply
  label says what happened ("looked at screen", "used tools").
- Speech-to-text: Groq Whisper, then Gemini, then Windows SAPI dictation (offline, weaker).
- The controller's "busy" is the running request task, so it can't get stuck after errors.
  Typing while recording discards the recording. Identical consecutive error cards are
  collapsed.
- Gemini returns HTTP 400 "API key not valid" for a bad key; it is classified as AUTH.
- Medium risk asks Yes/No; High risk requires typing CONFIRM. Shutdown/restart run with a
  60 s delay and can be cancelled.
- Screen, web, and file text is treated as information, never instructions (in every prompt).
- UI follows the owner's reference video: charcoal grid HUD, particle globe + clock when
  idle, cyan ring + screen-edge glow when active, reply card in the centre with the orb
  docked bottom-right. QPainter only (no blur effects), static layers cached as pixmaps,
  time-based easing; 60 fps when active, 30 when calm or if a frame takes >10 ms; stops
  when hidden, for Iris Xe. The owner liked this UI; keep its look.
- Closing the window hides to the tray; Quit is in the tray menu.
- Push-to-talk is Alt+` (vk 0xC0, or 0xDF on UK layouts). On Windows the backtick is
  swallowed while the chord is held and an unassigned key (0xE8) is tapped so releasing
  Alt doesn't focus the app's menu bar.
- Wake word: SAPI in-process recognizer on the default mic with a command grammar of
  `voice.wake_phrases`; only matches at or above `wake_min_confidence` count; paused
  whenever the app state isn't IDLE. The user says "Jarvis", pauses, then speaks; the
  hands-free recording ends after ~1.1 s of quiet (or 6 s with no speech, then dropped).
- When activated while the main window isn't the active window, a frameless, no-focus,
  always-on-top `OrbOverlay` shows at the bottom-right; it hides 4.5 s after IDLE (7 s
  after a failure). Screen capture hides it too.
- `router.preferred` (persisted as `providers.preferred` via `save_choice`, which edits
  only that key) puts one provider first; the others still back it up.
- Failure feedback: an ERROR entry or an ACTION starting with "✕"/"Declined" tints the
  window (red wash and edge), the orbs and the reply card red for 6 s or until the next
  request.
- Animation: one `FrameClock`; layout transitions are critically damped springs
  (`Tween`); orb layers are cached at exact size when still and at bucketed sizes while
  gliding (smooth scaled blits are ~30x slower than plain ones in the raster engine);
  moving thin lines use 1 px pens (Qt's fast antialiased path). Orb ~3 ms/frame.

## Known issues
- Groq and Ollama calls aren't exercised from CI; tests use `httpx.MockTransport` and fakes.
- SAPI TTS, dictation and the wake word, global hotkeys (and the backtick swallowing),
  the vsync clock, the corner overlay, the key dialog's registry write, and the desktop
  tools are untested on real Windows so far.
- The wake word needs a pause after "Jarvis"; words said in the same breath are lost.
- No settings window yet; edit `settings.json` in the data dir.

## Current status
JARVIS rebuild (replaces the Hitodama Gemini Live loop from PR #1): built, awaiting owner
testing on Windows.
