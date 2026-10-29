# CLAUDE.md: JARVIS

Voice assistant and agentic AI for the Windows 10/11 desktop. Current requirements:
`docs/JARVIS_SPEC.md`. `docs/MASTER_PROMPT.md` is the older "Wisp/Hitodama" spec; its
working rules and safety rules still apply where they don't conflict.

## Working rules
- Plan first, build, run ruff/mypy/pytest, give a manual test checklist, then wait for the
  owner to confirm on Windows.
- Push to `main` (owner's choice, 2026-10-24: `main` is JARVIS now; the old Wisp/Hitodama
  main is kept as the branch `old-main`). Never commit secrets.
- Verify model names and API shapes against provider docs before use. Model names live in
  config only.
- Every PC action is a tool in `jarvis/tools/builtin.py` with a `Risk`. There is no shell or
  registry tool, and none may be added. File tools (owner-approved in round 5) stay scoped
  by `tools/files.py`: user folders and non-system drives only, Recycle Bin instead of
  delete, no overwrite, never open runnable files. Never log audio, API keys, or dictated
  text (or window titles).
- Owner-approved exceptions (2026-10-24, "all four, each asks first"), and only these:
  `run_python` (HIGH: the code is shown and CONFIRM typed every run; Python only, `-I`,
  cwd `%LOCALAPPDATA%\JARVIS\code`, no secrets in its env, 30 s, AST refusal of
  subprocess/ctypes/winreg/pywin32/os.system/remove/exec...); plugins (a new or changed
  `.py` in the plugins folder is shown and needs CONFIRM, approval keyed by SHA-256, the
  approved text is what runs, output screened by Prompt Guard, tools default MEDIUM);
  `set_dark_mode` (MEDIUM, writes only the two HKCU Personalize theme values) and
  `set_wifi` (MEDIUM, WinRT radio API); the phone dashboard (off until a phone is paired
  with a one-time code). Owner, 2026-10-26 ("find the username box and type in the
  username, then the password box"): `fill_login` (MEDIUM, group `WORKFLOW_ONLY`: never
  offered to a model and refused in `_run_tool` unless a workflow step runs it; takes
  only a login's name; the login lives in Windows Credential Manager, `app/secrets.py`;
  types only into a window whose title contains the login's saved words; never logged).
  Still no generic shell, command-line or registry tool.
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
  single instance, `autostart.py` (Startup-folder shortcut `pythonw -m jarvis --hidden`,
  tray toggle "Start with Windows").
- `providers/`: `base.py` (types, `ProviderError`, `ErrorKind`, `ProviderState`),
  `openai_compat.py` (shared chat/transcribe/probe; `vocabulary` → Whisper `prompt`),
  `backends.py` (Groq, Gemini, OpenRouter with `pick_free_model`, Ollama),
  `network.py` (TCP reachability), `router.py` (priority, capability routing, bounded
  retries with backoff, fallback, `AllProvidersFailedError`).
- `app/keys.py`: API keys from the process env, then (Windows) the user env in
  `HKCU\Environment`; saving from the UI writes there too, like `setx`.
- `storage/db.py`: `HistoryStore` (SQLite conversations + messages, via `asyncio.to_thread`).
  `storage/keep.py`: `Keep` (`keep.sqlite3`: facts, reminders, watched topics, conversation
  summaries, small state) and `memory_prompt` (newest facts + an index of the rest).
  `storage/vault.py`: `Vault` (Markdown memory folder: Daily/, Notes/, JARVIS/Facts.md;
  `rank` = BM25 over ≤420-char chunks fused by reciprocal rank with `time_range` days;
  `recall_block`). `storage/memory.py`: `Memory` (vault + optional Hindsight: `recall`,
  `log`, `summary`, `note`, `facts_changed`). `storage/hindsight.py`: REST connector.
- `tools/`: `apps.py` (`AppIndex` of installed apps: Start menu .lnk/.url + shell:AppsFolder,
  minus uninstallers/help/links and blocked shells/system editors; fuzzy `best_match`),
  `media.py` (play_youtube: first `videoRenderer` on the results page, else the results;
  find_flights: Google Flights `?q=` URL; Steam tools; look_through_webcam MEDIUM),
  `messaging.py` (`send_whatsapp`: search, paste, Enter),
  `steam.py` (libraryfolders.vdf + appmanifest_*.acf; StateFlags≠4 = needs update;
  `steam://rungameid/<id>`, `steam://open/downloads`),
  `live.py` (keyless public APIs: `price_of` → Yahoo chart/search, CoinGecko →
  fawazahmed0, metals per gram, mfapi NAV; `convert` Frankfurter → fawazahmed0;
  `wiki_summary`, `define`, `air_quality`, `earthquakes`, `holidays`, `joke`;
  `register_live_tools` incl. `watch_market`, state `market_watch` in keep),
  `registry.py` (`Risk`, `ToolSpec`, arg validation), `desktop.py` (`WindowsDesktop`:
  approved launches with `shell=False`, pynput keys/typing, clipboard, lock/sleep, delayed
  shutdown), `files.py` (`FileArea`: allowed roots, blocked system folders, Recycle Bin via
  SHFileOperationW), `ui.py` (`WindowsUi`: UI Automation via the `uiautomation`
  package on one COM thread; `format_snapshot`, `blocked_reason`, `click_risk`), `shortcuts.py` (`SHORTCUTS` name → keys/risk/what, `parse_combo`
  refuses Win+R/Win+X/Ctrl+Alt+Del), `undo.py` (`UndoJournal`: each change records its own reverse), `web.py` (Open-Meteo
  weather, Google News RSS, DuckDuckGo HTML results), `personal.py` (memory, reminder,
  weather, look_up and watch_topic tools), `windows.py` (`WindowsWindows`: EnumWindows,
  raise, polite WM_CLOSE; never JARVIS or the shell), `system_ctl.py` (`WindowsSystem`:
  volume via pycaw, brightness via WMI, wallpaper via SystemParametersInfo),
  `slides.py` (`deck_command` local parser, `DeckWriter`: free models only, `parse_deck`,
  `make_presentation`), `deck_render.py` (python-pptx drawing: `THEMES`, `LAYOUTS`, `render`),
  `gmail_api.py` (`GmailAccounts`/`GmailAccount`: Gmail API, OAuth loopback + PKCE,
  `gmail.readonly` only, refresh tokens in Credential Manager), `zai.py` (`ZaiWriter`),
  `morning_brief.py` (`MorningBrief`, `register_brief_tools`), `brief_pdf.py` (Markdown →
  PDF via QTextDocument/QPdfWriter),
  `calendar_ics.py` (`Calendar`, `parse`, `events_between`), `day.py` (`my_day`,
  `calendar_events`), `tasks.py` (tasks/ideas tools, `parse_due`),
  `documents.py` (`read_text`: text, docx, xlsx, pptx, pdf via pypdf), `pc.py`
  (read_document, save_text, organize_desktop, close_app, set_volume/brightness/
  wallpaper, all undoable), `builtin.py` (the tool set; `ToolSpec.group`).
- `agent/`: `commands.py` ("use Gemini from now on" → provider switch, no model call),
  `prompts.py` (one system prompt; `wants_screen` regex; `context` block marked as
  information), `context.py` (`situation`: active window, other open windows, AI choice),
  `guard.py` (`PromptGuard`: Llama Prompt Guard 2 scores outside text for injections),
  `toolbudget.py` (`groups_for`: which tool groups a request gets), `routing.py`
  (`is_conversation`, `wants_cloud`, `KeepLocalWarm`), `background.py`
  (`Background`: reminders and other jobs → `Announce`), `routines.py` (`Briefing`, `TopicWatch`, `HardwareWatch`,
  `CheckIns`, `SessionSummaries`: jobs `Background` ticks every 10 s; `idle_seconds` via
  GetLastInputInfo), `workflows.py` (`Trigger`/`Step`/`Link`/`Workflow`, `ACTIONS` mapped to
  tools or a JARVIS request, `GROUPS`, `run_order`, `would_loop`, `condition_met`, `fill` for `{previous}`/`{step N}`/`{today}`, `WorkflowStore` (`workflows.json`),
  `parse_proposal`, `templates`, `WorkflowTriggers` job, `register_workflow_tools`),
  `planner.py` (`needs_plan`, planner prompt, `parse_plan` → `Task`s, `step_request`),
  `assistant.py` (keeps the last 8 actions for context; one turn:
  history, screen fast path, otherwise a tool loop with tools + `look_at_screen`,
  confirmations).
- `voice/`: `audio_io.py` (mic → WAV recorder, `EndOfSpeech` for hands-free), `tts.py`
  (`Speaker`: Kokoro neural voice when loaded, else SAPI, on its own thread; sentence
  pipelining, barge-in), `neural.py` (Kokoro-82M via onnxruntime + phonemizer/espeak-ng
  en-gb; model files from kokoro-onnx's model-files-v1.1 release, SHA-256 pinned),
  `spoken_text.py` (`speakable`: strips Markdown, think blocks, code, emoji; links →
  site name, paths → file name, lines → sentences, ₹/$/£/€ → words),
  `feed.py` (`SpeechFeed`: clause pieces for the voice, filled while a reply is written),
  `dictation.py` (`Dictation.paste`: refuses `blocked_reason` windows, clipboard paste,
  no Enter), `vocabulary.py` (`vocabulary`, `VocabularyRefresh` job), `whistle.py`
  (`WhistleStt`, `OfflineStt` chain), `confirm.py` (spoken yes/no), `hotkeys.py` (global push-to-talk; Windows hook swallows the
  chord's main key), `local_stt.py` (offline SAPI dictation fallback), `wake_word.py`
  (offline SAPI command grammar for "Jarvis").
- `remote/`: `dashboard.py` (`Dashboard`: asyncio HTTP server on `dashboard.port` 8765,
  started by "pair my phone"/tray or at launch once paired; 6-digit code, 10 min, 5 tries,
  one use; 32-byte token cookie, SHA-256 stored in `dashboard_tokens` (max 5); POSTs need
  `X-Jarvis: 1`; GET /, GET /state, POST /pair, /ask, /unpair; bodies ≤8 KB), `page.py`
  (one page, textContent only). Pairing is a local command, so the code never reaches an
  AI or the history.
- `controller.py`: glues UI, voice, and assistant; owns busy/listening.
- `ui/`: `theme.py`, `main_window.py` (HUD stage, tweens, layout), `tray.py`, `icons.py`,
  `workflows.py` (`WorkflowBuilder`: sidebar list, top bar name/Active/Save, `CanvasPane`
  (canvas + floating +, zoom/fit/tidy/undo/redo, Test workflow), `NodePanel` (+ panel by
  group, search), `Inspector` (live settings via `setting_field`), logs, `TemplateGallery`;
  `problems`, `warnings`, `layout`), `canvas.py` (`FlowScene`/`FlowView`/`NodeItem`/
  `LinkItem`: dotted grid, bezier links, ports, + stubs, drag-to-link, run status),
  `node_icons.py` (QPainter line icons, `GROUP_STYLE`),
  `widgets/`: `orb.py` (particle globe when idle, morphs into the glowing ring), `stage.py`
  (backdrop, top bar, clock/state caption, reply card, status strip, `Tween`), `panels.py`
  (provider/network/safety panels in the details overlay, command bar, confirm overlay),
  `chat.py` (history drawer), `keys.py` (API key overlay), `glass.py`, `camera.py` (one QtMultimedia
  photo, JPEG in memory, camera off straight after), `overlay.py`
  (always-on-top corner orb). `frame_clock.py`: one shared animation clock (vsync via
  `DwmFlush` on Windows, precise timer elsewhere).
- `assets/fonts/`: Orbitron, Rajdhani, Inter (OFL).

## Decisions
- Cloud first again (owner, 2026-10-26: "the local model is really very slow ... use all the
  free models from groq, then gemini and then openrouter"): `routing.local_first` now
  defaults to False; Ollama is last in the fallback. Groq fallbacks: minimax-m2.7,
  gpt-oss-120b, gpt-oss-20b (Llama 3.3 70B was shut down 2026-08-16, stripped from
  saved settings via `RETIRED_GROQ_MODELS`). OpenRouter `fallback_models` are the free
  slide models, plus up to 4 free tool-capable models listed at probe time
  (`free_models`); only `is_free` ids are used. The old local-first behaviour below
  still works with `routing.local_first: true`.
- Local first (owner, 2026-10-25, superseded by default on 2026-10-26: "all normal conversations should be routed here [local].
  only the ones that actually require cloud models ... to cloud"): `routing.local_first`.
  A request is conversation when `groups_for` finds no tool group, it isn't a screen
  question or a plan, and `is_conversation` sees no action verb at the start and nothing
  current/online/personal (weather, news, today, prices, my files...). Conversation goes
  to `router.local_chat` (Ollama only, no tools, `chat_prompt`: identity + "reply exactly
  NEEDS_TOOLS if this needs tools", `local_timeout_s` 90) on qwen3.5:4b, then gemma3:4b
  (Ollama `fallback_models`; Gemma gets no `reasoning_effort`). NEEDS_TOOLS, an empty
  reply, a timeout or an error → the normal cloud path with tools. A provider the user
  chose wins (`local_ready` is false unless preferred is None or ollama). `KeepLocalWarm`
  reloads the chat model with `keep_alive` (30m) every 20 min via Ollama's /api/generate.
  Speech-to-text, the screen, tools, plans, slides and session summaries stay cloud.
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
- Workflows (owner, 2026-10-25): trigger (phrase, time, startup, folder, manual) + up to
  20 steps from `ACTIONS`; tool-backed steps go through `_run_tool`, so each keeps its
  risk and confirmation; `confirm` steps that get a no skip the rest. The runner reuses
  the plan card (`PlanUpdated`); reply = headline + plan lines + shown outputs; spoken =
  headline + say/briefing/ask_jarvis outputs. Phrase match runs before everything else
  in `_handle` (exact phrase or "run the X workflow"); "whenever I…"/"create a workflow…"
  → one model call → `parse_proposal` → shown → MEDIUM confirm → saved. Time triggers
  fire once a day up to 30 min late (state `workflows_ran`), startup ones 10 s after
  start; both publish `RunWorkflow`, which the controller runs when IDLE (waits ≤15 min).
  `run_workflow` tool only queues. Tasks/ideas: `tasks` table in keep, tools
  `add_task`/`list_tasks`/`complete_task` (group "tasks"), `parse_due`. `starters()`
  (Morning Briefing, Daily Planner, End-of-Day Review) are saved on first start.
- Workflow canvas (owner, 2026-10-25: "something like [n8n] so its easier for me to add
  workflows ... cool templates and useful nodes like gmail, calender, notes"): steps
  have `id`/`x`/`y`, the trigger `x`/`y`; `Workflow.links` (`Link(src, port, dst)`,
  src may be `TRIGGER_ID`) is None for older/proposed workflows (= steps chained in
  order, `graph()`), written out on the first canvas edit. Runner: `run_order` (Kahn's,
  left to right), a step runs when a fired link leads to it; `{previous}` is the output
  of the node that led there; If (`condition`) fires true/false, `confirm` yes/no (a no
  with nothing linked still fails like before); off-path steps are SKIPPED "Not on this
  path" and left out of the reply. `PlanUpdated` carries `workflow_id`/`ids` so the
  builder lights nodes up (main subscribes `builder.show_run`). Agent nodes
  (`Action.request`: inbox summary, draft replies (draft_reply MEDIUM, Drafts only),
  emails → tasks, summarise, translate, action items) go through `_converse`. Colours
  stay in the HUD palette (no greens); icons are drawn, no image files. 55 nodes,
  20 templates, MAX_STEPS 30.
- Workflow builder round 2 (owner, 2026-10-26): group "Keyboard & screen": `keys`
  (lines "ctrl+c", "tab x3", "copy" = named shortcut, "type: …", "wait: 1"/"500ms"),
  `type_text`, `login` (→ `fill_login`), `ai_click` (agent request: read_ui →
  click_element, look_at_screen fallback, no typing); `json_actions` (Core: list of
  {keys|type|wait|open|tool+args|action+settings}, ≤60 calls, `json_calls`); `telegram`
  (`tools/telegram.py`, Bot API sendMessage, MEDIUM, env `TELEGRAM_BOT_TOKEN`/
  `TELEGRAM_CHAT_ID`, token never logged or shown, `find_chat` via getUpdates);
  "Ask JARVIS" is now "Prompt JARVIS". `Action.sequence` steps (`step_calls`) ask once
  for the whole step at the riskiest call's level (`_run_tool(approved=)`; HIGH still
  asks itself), an optional app is raised first (0.8 s for the foreground tracker).
  `Action.connection` (gmail/calendar/telegram/logins) shows a Connect card in the
  node settings: `ui/connections.py` (`Connections`, `ConnectDialog` saving with
  `keys.save_key`, secrets masked and never shown again, `LoginsDialog` →
  `LoginStore`). Builder tidy: sidebar search, "+ New workflow", one tool pill,
  settings in a card, `{ } Variables` tooltip, a shortcuts chip list instead of the
  HOW TO wall. Proposals: `proposal_prompt` lists nodes by group with outputs and asks
  for `links` `[from, port, to]` (step numbers, "trigger"); `parse_proposal` keeps sane
  links (known ports, no loops, from the trigger, else in order) and drops any
  password/token-like setting; `workflow_request` also takes "make me a … workflow",
  "I want a workflow…", "automate …".
- Workflows don't ask (owner, 2026-10-26: "i dont want it to ask for permission for any
  workflows"): `workflows.ask_first` (default False) → workflow tool steps and
  keys/typing/JSON steps run with `_run_tool(approved=Risk.MEDIUM)`, logins and sends
  included. HIGH steps still need CONFIRM typed, "Ask me before going on" nodes still
  ask, saving a workflow JARVIS proposed still asks, and actions a model chooses inside
  agent nodes (AI click, drafts…) keep their usual confirmations. Builder panels sit in
  `QSplitter`s (sidebar | canvas | settings, canvas over logs), sizes kept per session.
  Save is never greyed out: a press with something missing says what (state label) and
  goes there (`_point_to`: trigger field, the step, or the node panel); a phrase trigger
  with no phrase takes the workflow's name. Saved logins can be edited (pick one; a
  blank password keeps it); a rename updates `login` steps in saved workflows
  (`rename_login`).
- Gmail nodes check first (owner, 2026-10-29: the inbox summary said ✓ over the model's
  "I’m unable to reach your Gmail"): agent nodes with `connection == "gmail"` run
  `check_email` (count 1) before the model (`_mailbox_problem`); a failure fails the step
  with the tool's reason + "Open this node's Gmail Connect card", no unread mail
  finishes summary/tasks nodes with "No unread email" and no model call. `_CANNOT`
  accepts curly apostrophes. Testing a workflow logs a ⚠ line per unlinked step; the
  run note is "Not linked: nothing leads to it".
- Gmail API + morning brief (owner, 2026-10-29: "Integrate the official Gmail API ... OAuth
  2.0 ... minimum permissions ... read-only ... Never request my Gmail password"; "use z ai
  to send me a pdf of morning brief like this to my telegram ... my 2 gmail ids ... A
  short summary ... saved to jarvis memory"). `tools/gmail_api.py`: the user's own Google
  Cloud "Desktop app" OAuth client (`JARVIS_GOOGLE_CLIENT_ID`/`_SECRET`, saved like keys),
  Google's sign-in page in the browser, PKCE S256 + `state`, a one-shot listener on
  127.0.0.1:<random>, scope `gmail.readonly` only (no send/modify/compose); refresh tokens
  in Credential Manager (`secrets.WindowsTokens`, "JARVIS Gmail: <address>"), access
  tokens in memory; Remove revokes at Google. Several accounts; email tools number ids
  "n:id" when there are 2+; `mail_sources` prefers Gmail sign-ins, else the IMAP env
  mailbox; `draft_reply` on Gmail returns the text (read-only can't save drafts). The
  Gmail Connect card (`GmailDialog`) never asks for a password. `tools/zai.py`: Z.ai's
  OpenAI-shaped `/chat/completions`, `ZAI_API_KEY`, general endpoint then the Coding Plan
  one, `brief.models` (glm-5.2, glm-4.7-flash). `tools/morning_brief.py`: last 24 h of
  inbox mail per account (≤`max_emails`), tasks/reminders/calendar, Google News (India,
  RBI/SEBI, business, world, Fed/oil), Yahoo quotes (`MARKET_ROWS`), `brief.watchlist` +
  the node's list; each email and the news screened by Prompt Guard (flagged text
  withheld); Z.ai gets only sender, subject, time, read state, "Gmail n" and the first
  `excerpt_chars` (700): never the user's addresses or links (it cites E3/N5, `finish`
  swaps in links). The model writes the owner's 13-section layout; JARVIS inserts the
  market and system-health tables (`{{MARKETS}}`/`{{HEALTH}}`) from real data and a
  footer naming the Z.ai model; `<summary>` → `memory.note("Morning briefs")` + keep
  state `morning_brief_last`. PDF via Qt (`brief_pdf.markdown_pdf`, "xb", Documents\
  JARVIS Briefs), Telegram `sendDocument` with the summary as caption. Tool
  `morning_brief` (MEDIUM, group "brief"), node "Morning brief PDF to Telegram"
  (connections zai,gmail,telegram: the Inspector shows a card per service), template
  "Morning brief to Telegram" daily 08:00.
- Quick capture: `commands.capture_command` (idea:/add a task…/todo:/add X to my Y
  tasks/quick note:) → `add_task` or `save_note` "Inbox" in `_handle` before the deck
  check, no model (mode "capture"); a trailing due ("by friday", "tomorrow") is split
  off; text naming another app (Notion, Todoist, WhatsApp…) is left alone.
  `recall_memory` also lists matching tasks/ideas. More `wants_screen` phrasings
  ("explain this", "what should I click", "is this a scam").
- Writing (owner's 14 workflows): `tools/writer.py` `FreeWriter` (free OpenRouter
  `slides.models` → Groq when ≤5,500 chars → `router.chat` when ≤24K chars; `Written.credit`
  names the model). `tools/writing.py` (group "writing", screened): `study_document`
  (summary/key_points/action_items/answer/explain/study_notes; 'upload' = dropped/picked;
  `excerpt` of ≤400 PDF pages), `research` (DuckDuckGo → pages → `page_text` → Prompt
  Guard per page (flagged dropped, counted) → cited report; `with_sources` rebuilds the
  source list from citations in the body; saved Documents\JARVIS Research, opened),
  `write_document` (Markdown → `markdown_to_docx` via python-docx (MIT), "xb", Documents\
  JARVIS Writing). `paths.writing_dir/research_dir`.
- Email (owner's list: "summarise, find action items, draft replies for approval"):
  `tools/email_imap.py`, IMAP over TLS with env `JARVIS_EMAIL_ADDRESS`/`_PASSWORD`
  (app password)/`_IMAP` (host guessed for Gmail/Outlook/Yahoo/iCloud/Zoho). INBOX
  selected read-only, `BODY.PEEK[]`; `check_email`/`read_email` (group "email",
  screened), `draft_reply` MEDIUM → APPEND to the `\Drafts` folder with In-Reply-To.
  No SMTP anywhere. Content and addresses never logged. `spoken_question` speaks only a
  description's first line (the draft stays on screen).
- Decision/Troubleshooting: `prompts.guidance(text)` adds `_DECIDE` or `_TROUBLESHOOT` to
  the system/chat prompt (`extra=`). `tools/diagnostics.py` `check_pc(area)` (group
  "system", LOW, read-only): psutil uptime/CPU/memory/busiest by process name/disks/
  battery, DNS + TCP 443 to 1.1.1.1 and google, Windows System/Application error events
  (24 h, ≤10, source + id only) via win32evtlog.
- Watchtower (owner: "run monitoring locally… let users configure which folders and
  events"): `agent/watchtower.py`. Watches (folder resolved through `FileArea`, pattern,
  events new/changed, recursive, max 20) in keep state `watchtower`; `FolderWatcher`
  lists (os.scandir, ≤5000 files, never opens), first look silent, a change is reported
  on the first scan where it's unchanged since the one before; temp/partial downloads
  ignored. Background job every `interval_s` (30): Said kind "watch"; `on_change` →
  `RunWorkflow(file=)` for folder workflows (`folder_watches`, ids "wf-<id>",
  announce off; `{file}` variable). Deadline alerts once per stage (day: not in quiet
  hours / hour / overdue ≤7 days; state `deadline_alerts`), calendar heads-up
  `calendar_minutes` before (state `event_alerts`). Tools `watch_folder`,
  `stop_watching_folder`, `list_watches` (group "watch"). UI: `ui/watchtower.py`
  `WatchtowerPanel` + `AutomationWindow` (tabs Workflows/Watchtower); "open
  Watchtower" opens that tab; options saved with `save_choice("watchtower", …)`.
- Recall: `tools/recall.py` `recall_work(when, project)` (group "recall", screened):
  `period` (yesterday/today/last week/this week/last N days/weekday/ISO; "" = 7 days),
  keep summaries and tasks, `HistoryStore.messages_between` (user requests, project
  words first), `Vault.daily_text` (≤3 days, last 2,500 chars), vault search for the
  project, `recent_files` (Desktop/Documents/Downloads, depth ≤4, ≤6,000 dirs, skips
  node_modules/.git/venv/AppData; names only). `prompts._RECALL` guidance.
- Calendar: read-only iCal from env `JARVIS_CALENDAR_ICS` (a secret URL, never logged;
  `tools/calendar_ics.py`, own parser: TZID/UTC/all-day, RRULE daily/weekly BYDAY/monthly
  incl. 2TU/-1FR/yearly, INTERVAL/COUNT/UNTIL, EXDATE, moved instances; 10-min cache;
  tzdata on Windows). `tools/day.py`: `my_day(when, review)` and `calendar_events`
  (group "tasks", screened). Briefing adds calendar and due/overdue tasks. Reminders
  `repeat` (daily/weekdays/weekly/monthly; column added on open; `next_occurrence`).
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
  Manager, UAC, the Run box, JARVIS itself; password fields are never read, and only
  `fill_login` (workflows only) types into one;
  runnable files are never double-clicked. All four tools' output is screened by Prompt
  Guard. The module is imported on its worker thread after Qt starts, because it sets the
  process DPI mode on import.
- From Mark-LVI (github.com/FatihMakes/Mark-LVI, CC BY-NC 4.0: ideas reimplemented, no
  code copied; owner asked for all non-UI features on 2026-10-24, and chose "all four, each
  asks first" for the code runner, plugins, phone dashboard and dark mode/Wi-Fi).
  Feature map: /mnt/project-files/jarvis/mark-lvi-feature-map.md.
- Tool budget: requests carry only "core" tools plus the groups `groups_for` finds in the
  words (system, keys, memory, clipboard, power, files, ui, reminders, finance, info,
  media, code, camera) and the groups the
  previous request used. Keeps Groq's 8K TPM free tier usable as tools grow.
- Memory: `remember`/`recall_memory`/`forget`; the prompt carries ~900 chars of the newest
  facts plus an index of the other keys (by category, round robin), marked as facts, not
  instructions. Nothing is deleted automatically.
- Long-term memory (owner, 2026-10-24: "obsidian or something ... so it doesn't forget a
  single thing"): `memory.vault` folder (default Documents\JARVIS Memory; data_dir/memory
  with `JARVIS_DATA_DIR`). Every answered exchange is appended to Daily/<date>.md in a
  background task (WhatsApp sends as a one-line summary; diagnostics, pairing and
  dictation never). `checked_context(query)` adds ≤`recall_chars` (700) of matching
  passages for requests of 3+ words, screened by Prompt Guard (dropped if flagged). The
  vault is the user's own data store, like history; the log files still never hold
  their words. `recall_memory` also searches the vault; `save_note` (LOW, group memory)
  writes Notes/. `SessionSummaries` asks for JSON {summary, facts}; the summary goes to
  keep and the daily note, facts (≤8, user-stated only) to `keep.remember` when
  `memory.learn_facts`. Facts.md is rewritten on remember/forget/learn and at start.
  Hindsight (MIT) was judged too heavy to bundle (Postgres, embeddings, reranker, an LLM
  call per memory); its ideas are reimplemented, and `memory.hindsight_url` adds its
  REST API (retain + recall merged in, failures ignored). Open Glean was not used: it's
  a web app over Hydra DB's paid cloud.
- Live data (owner asked for public-apis features, 2026-10-24): groups "finance"
  (`get_price`, `convert_currency`, `watch_market`) and "info" (`wikipedia`,
  `define_word`, `air_quality`, `earthquakes`, `public_holidays`, `tell_joke`, all
  Prompt-Guard screened). Home currency from `tools.news_locale` (en-IN → INR; Indian
  shares prefer .NS). `MarketWatch` checks every 30 min outside quiet hours and speaks a
  move ≥ `move` % from the last mentioned price (then re-marks). Briefing adds
  `routines.briefing_markets` (["nifty", "sensex", "gold"]). The sandbox can't reach these
  APIs; shapes come from their docs (Frankfurter, fawazahmed0 and mfapi were checked live
  earlier) and tests use MockTransport.
- Interrupt (owner, 2026-10-24): `controller.interrupt()` from Esc (`window.on_escape`),
  the tray's Stop JARVIS, push-to-talk, the wake word (kept listening while busy:
  `voice.wake_while_busy`) or a typed request cancels the running task and speech
  ("Stopped.", mode "stopped"); "stop"/"never mind"/"cancel that" (`is_stop_command`)
  needs no AI. A confirmation in progress is never cancelled (only its speech stops).
  `voice.interrupt` turns it off.
- Dictation (Handy, MIT, reimplemented): `hotkeys.dictation` (Alt+Shift+`) records
  while held, transcribes, pastes into the foreground window (not JARVIS, shells,
  terminals or other `blocked_reason` windows), restores the clipboard, never Enter, no
  AI, no history. Ignored while a request runs. Chords need exactly their modifiers, so
  Alt+Shift+` doesn't also fire Alt+`.
- App clicks (cua, MIT, ideas only): plain clicks on Button/MenuItem/Hyperlink/
  SplitButton use Invoke, CheckBox Toggle, RadioButton/TabItem Select, mouse as fallback
  (ListItems stay mouse clicks). After click/type the tool compares readings: identical
  elements → `effect: "no visible change"` plus a hint; typed text not in any value →
  `typed_check: "not seen"`.
- OmniVoice (k2-fsa, Apache-2.0) was assessed for non-English speech and not added:
  torch plus a 0.6B model is too heavy for the owner's Iris Xe laptop on CPU, and
  SAPI voices already cover installed languages.
- Undo (`undo_last`, LOW): reverses JARVIS's own last move/rename/copy (copy → Recycle
  Bin)/new folder (only while empty)/new text file (→ Recycle Bin)/desktop tidy-up (one
  step)/volume, mute, brightness or wallpaper change, session only, 30 steps.
- PowerPoint decks (owner, 2026-10-24: "only use free models", "dont use bad models like
  gemini"): `make_presentation` (LOW, group "slides") makes one JSON call (`DECK_PROMPT`,
  `json_mode`) to `slides.models` on OpenRouter (nemotron-3-ultra, nemotron-3-super,
  dots-3-note-preview, laguna-s-2.1, openrouter/free; `is_free`: ids ending ":free" or
  "openrouter/free", anything else is dropped; Inkling's free tier answers 403 outside
  agent apps and Qwen 3.8's ended, so `RETIRED_SLIDE_MODELS` strips them from saved
  settings; a 404 or a 403 "only available…" skips that model for the session), then Groq `openai/gpt-oss-120b` only when the prompt is ≤5,500
  chars (8K TPM). Never Gemini or Ollama. `parse_deck` cleans the JSON and downgrades a
  layout missing its content to bullets; `deck_render` draws every slide from shapes on a
  blank 16:9 layout (6 themes, 13 layouts incl. agenda, statement and native editable
  charts: column/bar/line/pie/doughnut; emoji icons in Segoe UI Emoji badges; "Lead:
  detail" bullets get a bold lead; notes), `open("xb")` so it never overwrites. The prompt
  asks for an `outline` first (ignored when parsing) so the model plans the story.
  Sources are read with `read_text` (400 PDF pages) and sampled by `excerpt` (12 segments,
  120K chars). "Upload" = a file dropped on the window in the last 10 min, else a
  QFileDialog. Clear requests go through `deck_command` without a chat model (mode
  "slides"); the reply names the model that actually wrote the deck.
- `open_app` raises an already-open window instead of launching again, and after
  launching waits up to 8 s for a window, so the reply says whether it really appeared.
  `close_app` sends WM_CLOSE (like the X) and reports windows still open (unsaved work).
- `save_text` creates new text files only (.txt/.md/.csv/.json/...), never overwrites.
  `organize_desktop` (MEDIUM) moves loose desktop files into type folders; shortcuts,
  folders and name clashes stay put.
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
- Speech-to-text: Groq Whisper, then Gemini, then offline: Cactus Whistle (Apache-2.0;
  only if `needle` is importable, `voice.whistle`; NEEDLE_TELEMETRY=0 set before import;
  en/de/fr/es/it/nl/pl; vocabulary passed as keywords), then Windows SAPI dictation.
  `voice.custom_words` plus capitalised names in people/places/about_user/projects facts
  (≤60, refreshed every 5 min) go to Whisper's `prompt` and Gemini's transcription text.
- The controller's "busy" is the running request task, so it can't get stuck after errors.
  Typing while recording discards the recording. Identical consecutive error cards are
  collapsed.
- Gemini returns HTTP 400 "API key not valid" for a bad key; it is classified as AUTH.
  FAILED_PRECONDITION from Gemini is account/billing side, not ours.
- Models: Groq `qwen/qwen3.8-27b` (owner's choice; Preview, free tier 8K tokens/min,
  `reasoning_effort: "none"`), then `minimaxai/minimax-m2.7` (Preview, agentic; owner asked
  for "kimi k3 or something light but very good", 2026-10-24; Groq has no Kimi; no
  reasoning field, its <think> is stripped), `openai/gpt-oss-120b` and `openai/gpt-oss-20b` ("low") for chat when qwen is
  rate-limited; STT
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
  503 or another 400 (Groq's `tool_use_failed` when a model garbles a tool call) → next
  model now; timeout, auth, billing (FAILED_PRECONDITION/PERMISSION_DENIED) →
  raise (the router moves to the next provider). Each model's failure is logged with
  the provider's error text (≤200 chars). An error naming several models lists each.
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
- Fallback is Groq → Gemini → OpenRouter → Ollama (or the preferred one first). OpenRouter
  is optional (`OPENROUTER_API_KEY`; skipped and listed when missing), default
  `nvidia/nemotron-3.5-super-vl:free` for chat and vision; if a configured free model
  vanishes, the first listed free tool-capable model is used. Kimi K3
  (`moonshotai/kimi-k3`) is paid there and is only a documented option. A saved priority
  equal to the old default gets openrouter inserted. Only 5xx is retried on
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
- Failure loops: if look_at_screen fails (no provider could see), it is removed from the
  turn's tools and the reply is red; three tool rounds in a row with every call failing
  stop the turn with "I stopped after 3 attempts in a row failed. The last one: …"
  (red). A usable provider with no vision model is listed in the failure ("Ollama: no
  model that can see images is installed"). Ollama trusts the configured vision model
  (qwen3.5:4b) when it's installed even if `/api/show` lists no capabilities.
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
  off. A spoken confirm never raises the window (`ask(request, focus=False)`): raising it
  mid-WhatsApp broke the owner's flow; `bring_forward` raises it only when the answer
  can't be heard. Owner (2026-10-24): no asking for typing, copy, cut, paste, undo or
  ordinary keys; `combo_risk` keeps Enter/Delete/Alt+F4/Ctrl+W/Ctrl+S/Ctrl+Q MEDIUM, and
  sending (type_into submit, Send-style clicks) still asks once. WhatsApp (owner,
  2026-10-24: the UI Automation loop "is not working like intended") now uses Mark-LVI's
  method, reimplemented in `tools/messaging.py`: `send_whatsapp(contact, message)`
  (MEDIUM, one confirm) raises/opens WhatsApp, Ctrl+F, types the contact, Enter, pastes
  the message via the clipboard (restored after), Enter. Nothing is pressed unless the
  foreground title contains "WhatsApp" (checked before searching and before pasting);
  a UIA read after the chat opens checks the contact's name (search box ignored) but
  never blocks; a mismatch is said in the reply. WhatsApp sends never need a model:
  `commands.whatsapp_command` parses "send hi to Mom on WhatsApp", "message Mom on
  WhatsApp saying hi", "tell Arjun … on WhatsApp", "open WhatsApp and send …" and runs
  the tool directly (one confirm, mode "whatsapp"); with no contact it asks "Who should I
  send … to?" and takes the next short answer (`contact_reply`; "cancel" drops it).
  Send-style WhatsApp requests that still reach a model get no look_at_screen and no
  read_ui/click/type/scroll tools. High risk requires typing CONFIRM. Shutdown/restart run with a
  60 s delay and can be cancelled.
- Screen, web, and file text is treated as information, never instructions (in every prompt).
- UI follows the owner's reference video: charcoal grid HUD, particle globe + clock when
  idle, cyan ring + screen-edge glow when active, reply card in the centre with the orb
  docked bottom-right. QPainter only (no blur effects), static layers cached as pixmaps,
  time-based easing; 60 fps when active, 30 when calm or if a frame takes >10 ms; stops
  when hidden, for Iris Xe. The owner liked this UI; keep its look.
- Voice lag (owner, 2026-10-25: "laggy and late to respond"; measured: the first piece
  is ready in ~0.35 s, but the next long sentence left a ~2 s gap, and the whole reply was
  awaited): `Speaker` reads a `SpeechFeed` (voice/feed.py) of whole clauses; the first
  piece is one clause, each next one is sized from the learned seconds-per-char so it's
  made while the current one plays (`_budget`, 40-280 chars). `open_stream` speaks a
  local reply while it's written: `router.local_chat(on_text=)` streams SSE from Ollama
  (`stream_chat`; next model only before any text), `_Relay` holds text until it can't
  be NEEDS_TOOLS (or a think block), the controller moves to SPEAKING on the first piece
  and doesn't speak `Reply.streamed` replies again. `voice.speak_while_writing`,
  `voice.end_silence_s`. Samples: /mnt/project-files/jarvis/voice-samples/.
- Fluent pieces (owner, 2026-10-29: the voice "reads very bad, not fluent"): pieces were
  cut at commas mid-sentence ("Good morning," / "sir. It's…"), the comma was stripped and
  `speakable` added a full stop, so every clause ended like a sentence, then 0.22 s of
  silence. Now a piece keeps its comma (`speakable` leaves ,;: endings), runs stop at the
  last full stop when one fits (`_clauses_upto`), the first piece is the whole first
  sentence when ≤60 chars, and only sentence ends get 0.22 s (a comma 0.06 s).
  `voice.neural_voice` takes mixes ("bm_george+bm_fable", "bm_george*6+am_michael*4",
  `neural.blend`). Comparison samples: voice-samples/round-2/.
- Voice (owner asked for a free, unlimited JARVIS-like voice, 2026-10-24): local Kokoro
  `bm_george` at speed 1.15 (0.95 felt slow; owner picked 1.15 over 1.05 on 2026-10-25), 0.22 s between sentences,
  first piece cut at its first comma when over 70 chars so audio starts sooner, a
  warm-up synthesis at load, low-latency output stream; fp32 model (325 MB) because it ran
  3x faster than int8 on x86 CPUs (RTF ~0.33 vs ~1). Downloaded in the background on
  first start (`voice.download_voice`), cancelled on quit; SAPI speaks meanwhile and is
  the fallback. No cloning of the actor's voice. Kokoro-onnx itself isn't a dependency
  (it caps Python <3.14); `neural.py` runs the model directly (vocab from the model's
  `kokoro_config` metadata). Piper was considered; its voices are on Hugging Face,
  which the build sandbox can't reach to pin checksums.
- Personality: `_IDENTITY` in prompts.py is the owner's JARVIS brief, condensed (British
  English, composed, concise, dry wit, lead with the result, never invent results).
  Plan summaries read "All three tasks are complete." / "One of two tasks complete;
  one didn't go through."
- Routines (`routines` settings, each can be turned off): briefing once a day between 4
  and 12 when the user has touched the PC in the last 2 min (date, home-city weather,
  today's reminders, unmentioned summaries, `briefing_headlines` top stories; state
  `briefing_day`); topic news checked every 20 h, first check silent, only a changed top
  headline is said; battery at `battery_low` and again at half that (reset when
  plugged), CPU/RAM ≥90% for 12 readings (2 min), repeated at most every 30 min; no CPU
  temperature (Windows only reports it to admins); check-in after `check_in_hours` with
  no 5-min break; session summaries one LLM call (`SUMMARY_PROMPT`) after 15 min of
  quiet if the session had 2+ user turns (`summarised_upto`; on first run existing
  history is skipped; on failure retried after 30 min). Check-ins and topic news respect
  quiet hours (`quiet_from`/`quiet_until`). Watched topics keep the user's casing.
- Small extras (batch 4): a spoken request still working after 2.5 s gets "On it." /
  "Right away." / "One moment." (`voice.acknowledge`; never while listening or
  speaking); during follow-up listening a transcript of 4+ words that is ≥80% JARVIS's
  last spoken words is ignored (`is_echo`); `<think>` blocks are stripped from shown and
  spoken replies; the context carries an "About you" line (version, current voice);
  replies mostly in a non-Latin script (Hindi, Malayalam, Arabic...) are spoken by a
  SAPI voice of that language if installed (Kokoro is English only). Webcam photos go
  to the vision route once, after a Yes, and are never stored. No assistant/user name
  settings: memory ("call me ...") covers it.
- Closing the window hides to the tray; Quit is in the tray menu.
- Push-to-talk is Alt+` (vk 0xC0, or 0xDF on UK layouts). On Windows the backtick is
  swallowed while the chord is held and an unassigned key (0xE8) is tapped so releasing
  Alt doesn't focus the app's menu bar.
- Wake word: SAPI in-process recognizer on the default mic with a command grammar of
  `voice.wake_phrases`; only matches at or above `wake_min_confidence` count; paused
  whenever the app state isn't IDLE. The user says "Jarvis", pauses, then speaks; the
  hands-free recording ends after `voice.end_silence_s` (0.6 s, was 1.1) of quiet (or 6 s
  with no speech, then dropped).
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
- SAPI TTS, the Kokoro voice's playback and download on Windows, dictation and the wake word, global hotkeys (and the backtick swallowing),
  the vsync clock, the corner overlay, the key dialog's registry write, and the desktop
  tools are untested on real Windows so far.
- The wake word needs a pause after "Jarvis"; words said in the same breath are lost.
- Round 11 (memory vault, live data tools, interrupt, OpenRouter, dictation, Whistle,
  pattern clicks) is untested on Windows; the live APIs were mocked (the sandbox can't
  reach them).
- The UI tools (UI Automation, clicks, typing) are untested on real Windows; apps that
  draw their own UI (games, some Electron/canvas apps) expose few controls, so the model
  falls back to `look_at_screen`. A long app task uses many tokens (Groq's free tier
  then hands over to the backup models).
- The deck maker, the file picker and drag-and-drop are untested on Windows; OpenRouter's
  free model list changes, so `slides.models` may need updating (`openrouter/free` is the
  catch-all).
- The workflow canvas (dragging, linking, zoom, live node status) is untested on real
  Windows; offscreen renders and scripted tests only.
- No settings window yet; edit `settings.json` in the data dir.
- File tools, clipboard, the Recycle Bin call and window-title context are untested on
  real Windows. `press_keys`/`type_text` could still drive an address bar; both ask first
  and show what they'll do.

## Current status
JARVIS rebuild (replaces the Hitodama Gemini Live loop from PR #1): built, awaiting owner
testing on Windows.
