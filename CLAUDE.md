# CLAUDE.md: JARVIS

Voice assistant and agentic AI for the Windows 10/11 desktop. Current requirements:
`docs/JARVIS_SPEC.md`. `docs/MASTER_PROMPT.md` is the older "Wisp/Hitodama" spec; its
working rules and safety rules still apply where they don't conflict.

## Working rules
- Plan first, build, run ruff/mypy/pytest, give a manual test checklist, then wait for the
  owner to confirm on Windows.
- Push to `main` (owner's choice, 2026-10-24: `main` is JARVIS now; the old Wisp/Hitodama
  main is tagged `old-main`). Never commit secrets.
- Verify model names and API shapes against provider docs before use. Model names live in
  config only.
- Every PC action is a tool in `jarvis/tools/builtin.py` with a `Risk`. There is no shell or
  registry tool, and none may be added. File tools (owner-approved in round 5) stay scoped
  by `tools/files.py`: user folders and non-system drives only, Recycle Bin instead of
  delete, no overwrite, never open runnable files. Never log audio, API keys, or dictated
  text (or window titles).
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
  `storage/keep.py`: `Keep` (`keep.sqlite3`: facts, reminders, watched topics, conversation
  summaries, small state) and `memory_prompt` (newest facts + an index of the rest).
- `tools/`: `apps.py` (`AppIndex` of installed apps: Start menu .lnk/.url + shell:AppsFolder,
  minus uninstallers/help/links and blocked shells/system editors; fuzzy `best_match`),
  `registry.py` (`Risk`, `ToolSpec`, arg validation), `desktop.py` (`WindowsDesktop`:
  approved launches with `shell=False`, pynput keys/typing, clipboard, lock/sleep, delayed
  shutdown), `files.py` (`FileArea`: allowed roots, blocked system folders, Recycle Bin via
  SHFileOperationW), `ui.py` (`WindowsUi`: UI Automation via the `uiautomation`
  package on one COM thread; `format_snapshot`, `blocked_reason`, `click_risk`), `shortcuts.py` (`SHORTCUTS` name → keys/risk/what, `parse_combo`
  refuses Win+R/Win+X/Ctrl+Alt+Del), `undo.py` (`UndoJournal`: each change records its own reverse), `web.py` (Open-Meteo
  weather, Google News RSS, DuckDuckGo HTML results), `personal.py` (memory, reminder,
  weather, look_up and watch_topic tools), `builtin.py` (the tool set; `ToolSpec.group`).
- `agent/`: `commands.py` ("use Gemini from now on" → provider switch, no model call),
  `prompts.py` (one system prompt; `wants_screen` regex; `context` block marked as
  information), `context.py` (`situation`: active window, other open windows, AI choice),
  `guard.py` (`PromptGuard`: Llama Prompt Guard 2 scores outside text for injections),
  `toolbudget.py` (`groups_for`: which tool groups a request gets), `background.py`
  (`Background`: reminders and other jobs → `Announce`), `planner.py` (`needs_plan`, planner prompt, `parse_plan` → `Task`s, `step_request`),
  `assistant.py` (keeps the last 8 actions for context; one turn:
  history, screen fast path, otherwise a tool loop with tools + `look_at_screen`,
  confirmations).
- `voice/`: `audio_io.py` (mic → WAV recorder, `EndOfSpeech` for hands-free), `tts.py`
  (SAPI on its own thread), `confirm.py` (spoken yes/no), `hotkeys.py` (global push-to-talk; Windows hook swallows the
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
- Ollama: chat and vision model `qwen3.5:4b` (owner's pick after the self-test; falls back
  to `qwen3.5:latest` if only that is installed; timeout 240 s) with `reasoning_effort:
  "none"` so it doesn't think; if the vision model is missing, any installed model whose
  `/api/show` capabilities include `vision`.
- Agentic multi-task requests: a long request (`needs_plan`: 30+ words, or 9+ words with a
  joiner such as "then"/"after that"/"also"/";"/numbering, or 2+ commas) gets one planner
  call (no tools, JSON `{"tasks": [{"title", "do"}]}`, max 8, never adds tasks). With 2+
  tasks, each runs in turn through the normal tool loop (`_converse(history=False)` with
  `step_request`: this task, the whole request as background, results so far), with the
  usual confirmations. `PlanUpdated` (steps with pending/running/done/failed/skipped)
  drives the reply card ("PLAN · 3 TASKS · STEP 2/3", ○ ✱ ✓ ✕ -) and the caption/corner
  orb ("STEP 2/3" + title). A failed task (tool failed, declined, blocked, "I can't…")
  doesn't stop the rest; no provider answering does (the rest are skipped). The final
  ASSISTANT entry is the summary (mode "plan"), red if any task failed; spoken replies say
  only the headline and failures. One task, or an unparseable plan, takes the normal path.
- Operating apps (computer use): `read_ui` lists a window's controls as numbered lines
  (`[3] Edit "Type a message"`), and `click_element` / `type_into` / `scroll_ui` act by
  number and return the window afterwards, so the model works look → act → check
  (`max_tool_rounds` 15). Only the newest window reading stays in the messages
  (`trim_old_windows`); a reading is capped at ~3,200 chars (plain text dropped first).
  Clicks are mouse clicks at the element's centre after bringing the window to the front
  (Invoke pattern if it has no rectangle); typing focuses the element and types with
  pynput. Risk: LOW, except clicks on Send/Pay/Delete/Call/Save-style names and
  double-clicks in Explorer (MEDIUM), and `type_into(submit=True)` (Enter, MEDIUM).
  Never read or driven: shells, terminals, regedit, mmc, Settings, Control Panel, Task
  Manager, UAC, the Run box, JARVIS itself; password fields are never read or typed;
  runnable files are never double-clicked. All four tools' output is screened by Prompt
  Guard. The module is imported on its worker thread after Qt starts, because it sets the
  process DPI mode on import.
- From Mark-LVI (github.com/FatihMakes/Mark-LVI, CC BY-NC 4.0: ideas reimplemented, no
  code copied; owner asked for all non-UI features on 2026-10-24, and chose "all four, each
  asks first" for the code runner, plugins, phone dashboard and dark mode/Wi-Fi).
  Feature map: /mnt/project-files/jarvis/mark-lvi-feature-map.md.
- Tool budget: requests carry only "core" tools plus the groups `groups_for` finds in the
  words (system, keys, memory, clipboard, power, files, ui, reminders) and the groups the
  previous request used. Keeps Groq's 8K TPM free tier usable as tools grow.
- Memory: `remember`/`recall_memory`/`forget`; the prompt carries ~900 chars of the newest
  facts plus an index of the other keys (by category, round robin), marked as facts, not
  instructions. Nothing is deleted automatically.
- Undo (`undo_last`, LOW): reverses JARVIS's own last move/rename/copy (copy → Recycle
  Bin)/new folder (only while empty), session only, 30 steps.
- Reminders live in `keep.sqlite3`, not Task Scheduler; `Background` checks every 10 s,
  publishes a ChatEntry (mode "reminder") and `Announce`; the controller speaks
  announcements only when IDLE and not recording (busy while speaking). Missed ones say
  when they were due.
- Web: `get_weather` (Open-Meteo, home place from memory or `tools.home_place`),
  `look_up` (search or news; `tools.news_locale`), `watch_topic`. `look_up` and
  `recall_memory` output is screened by Prompt Guard.
- No modes. Screen questions (regex `wants_screen`) go straight to vision; everything else
  goes to the agent with all tools plus `look_at_screen`, so the model decides. The reply
  label says what happened ("looked at screen", "used tools").
- Speech-to-text: Groq Whisper, then Gemini, then Windows SAPI dictation (offline, weaker).
- The controller's "busy" is the running request task, so it can't get stuck after errors.
  Typing while recording discards the recording. Identical consecutive error cards are
  collapsed.
- Gemini returns HTTP 400 "API key not valid" for a bad key; it is classified as AUTH.
  FAILED_PRECONDITION from Gemini is account/billing side, not ours.
- Models: Groq `qwen/qwen3.8-27b` (owner's choice; Preview, free tier 8K tokens/min,
  `reasoning_effort: "none"`), then `openai/gpt-oss-120b` ("low") and
  `llama-3.3-70b-versatile` (no reasoning field) for chat when qwen is rate-limited; STT
  `whisper-large-v3-turbo` with `verbose_json`, dropping segments with no_speech_prob ≥ 0.6
  or avg_logprob ≤ -1 (Whisper invents "I'm going to go." from silence). Gemini (the
  backup, so fast first) `gemini-3.5-flash-lite` ("low"), then 3.8 Flash (timed out on the
  owner's key in the self-test), 3.6 Flash, 2.5 Flash ("none" only there; legacy access).
  `qwen3.5:latest` is the 9B (~7 GB) and took 33 s for "OK" on the owner's Iris Xe
  laptop, hence `qwen3.5:4b` as the local default. Every request sends
  `max_tokens` (`max_output_tokens`, 2048): without it Groq counted the model's full output
  limit against 8K TPM and answered 429 every time. Tool text is kept small (no shortcut
  catalogue, path help once in the prompt): ~2.3K tokens.
- Model fallback inside a provider: 404/"model not found" → model dropped for the session;
  429 → model cools down 60 s; timeout → model cools down 5 min (and the router moves on);
  503 → next model now; timeout, auth, billing, bad request →
  raise (the router moves to the next provider). An error naming several models lists each.
  Replies cut off at the limit (`finish_reason: length`) end with "…".
- Diagnostics: "test the AI providers" / "run diagnostics" (or the TEST AI PROVIDERS button)
  probes each provider and pings its models in turn ("Reply with just the word OK.", 30 s
  each, no tools) until one answers; that model is adopted for chat. Stops early on
  auth/billing errors. Reports model and time, or each model's exact error and whose side
  it is on (`Diagnosis.blame`: account, service, network, setup, jarvis).
- settings.json may override single provider fields (`{"providers": {"ollama":
  {"chat_model": "qwen3.5:4b"}}}`); the rest is filled from the defaults.
- Prompt Guard (owner asked for `meta-llama/llama-prompt-guard-2-86m` on Groq): it is a
  classifier, not a chat model, so it screens text, never answers. `providers.groq.
  guard_model`/`guard_threshold` (0.8). Results of `UNTRUSTED_TOOLS` (clipboard, folder
  listing, find, look_at_screen) and the window-title context are scored in ≤1800-char
  chunks; flagged tool text is withheld (ACTION "✕ Blocked…", reply marked failed, red),
  flagged titles are dropped. Guard errors fail open (logged). The user's own words are
  never screened.
- Fallback is Groq → Gemini → Ollama (or the preferred one first). Only 5xx is retried on
  the same provider (1 + `max_retries`, now 1; timeouts 20 s); a timeout, network error, auth, billing
  or bad request moves to the next provider at once (retrying timeouts used to take
  minutes before switching). The router publishes `activity` ("Groq didn't answer, asking
  Gemini…") and `skipped`; the caption, corner orb and a notice show them, and the header
  names the provider tried first or the one that last answered (never a different one).
- Gemini: a configured model missing from `/models` at probe time is swapped for the first
  listed fallback; the working model is remembered per capability. Tool calls keep Gemini's `extra_content` thought
  signature for Gemini (placeholder `skip_thought_signature_validator` for calls made by
  another provider) and have it stripped for Groq/Ollama. Google's error status
  (FAILED_PRECONDITION etc.) is shown with a plain hint.
- Any unexpected exception in a turn becomes an ERROR entry (so the UI goes red), never a
  silent return to idle. Chat that fails after all retries (1 + `max_retries`) on every
  provider is an ERROR too.
- Qt 6 sets per-monitor DPI awareness itself; calling SetProcessDpiAwareness first makes
  Qt log "Access is denied", so we don't.
- Follow-up conversation: a wake word starts a conversation; after each reply the
  controller listens again hands-free with `no_speech_s = voice.follow_up_s` (30). Silence
  for that long, typing, a too-short clip or a mic error ends it (`ConversationChanged`).
  The window keeps the reply card up and shows "STILL LISTENING" meanwhile. Push-to-talk
  doesn't start a conversation.
- Medium risk asks Yes/No out loud (owner's ask): `AppController.confirm` speaks
  `spoken_question(description)`, records one hands-free answer (6 s of silence = no),
  `parse_yes_no` (any no wins; its own question echoed back isn't an answer), asks once
  more if unclear, then takes it as no. The Yes/No buttons stay up and the first answer
  wins; with no mic or speech-to-text the buttons decide. `voice.voice_confirm` turns it
  off. High risk requires typing CONFIRM. Shutdown/restart run with a
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
- `open_app` takes any name: `approved_apps` first, then the installed-app index (LOW risk).
  It opens the shortcut file (must be inside a Start menu folder) or
  `shell:AppsFolder\<AUMID>` with `os.startfile`; never a command line. Shells and system
  editors are excluded from the index so the typing tool can't drive them. The prompt
  tells the model to always call open_app rather than refusing.
- Failure feedback: an ERROR entry, an ASSISTANT entry with `failed` (a tool failed or was
  declined with no later success, or the reply opens with "I can't…"; see
  `says_it_cannot`), or an ACTION starting with "✕"/"Declined" tints the
  window (red wash and edge), the orbs and the reply card red for 6 s or until the next
  request. The corner overlay turns red too (`OrbOverlay.set_alarm`), and is shown for
  7 s on a failure if the main window isn't in front. Listening doesn't clear it.
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
- The UI tools (UI Automation, clicks, typing) are untested on real Windows; apps that
  draw their own UI (games, some Electron/canvas apps) expose few controls, so the model
  falls back to `look_at_screen`. A long app task uses many tokens (Groq's free tier
  then hands over to the backup models).
- No settings window yet; edit `settings.json` in the data dir.
- File tools, clipboard, the Recycle Bin call and window-title context are untested on
  real Windows. `press_keys`/`type_text` could still drive an address bar; both ask first
  and show what they'll do.

## Current status
JARVIS rebuild (replaces the Hitodama Gemini Live loop from PR #1): built, awaiting owner
testing on Windows.
