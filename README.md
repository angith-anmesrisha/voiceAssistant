# JARVIS

A voice assistant and safe desktop agent for Windows 10/11, with a futuristic HUD.
Talk or type; JARVIS answers using **Groq** (primary), **Gemini** (backup) or **Ollama**
(offline on your PC), and can carry out narrowly scoped desktop actions with your permission.

![JARVIS idle](docs/renders/01-idle.png)

More renders (listening, thinking, speaking, executing, confirmations, offline) are in
[`docs/renders`](docs/renders). Regenerate them with
`QT_QPA_PLATFORM=offscreen python scripts/render_ui.py`.

## Setup (Windows)

1. Install **Python 3.12 or newer (64-bit; 3.12, 3.13 and 3.14 are tested)** from python.org (tick "Add python.exe to PATH").
2. In this folder:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -e ".[dev]"
   ```

3. Add API keys. The easiest way: start JARVIS and click the **key button** at the top
   of the AI PROVIDERS panel, then paste your keys. Or from a terminal:

   ```powershell
   setx GROQ_API_KEY "gsk_..."      # https://console.groq.com/keys  (primary, voice)
   setx GEMINI_API_KEY "AIza..."    # https://aistudio.google.com/apikey  (backup, reads the screen)
   ```

   Either way the keys are stored only as your Windows user environment variables, never
   in JARVIS files or logs, and JARVIS picks them up without a restart. Use either key,
   or both.

4. Optional, for offline use: install [Ollama](https://ollama.com) and run
   `ollama pull qwen3.5`. JARVIS uses `qwen3.5:latest` for chat and, since it can see
   images, for screen questions too (thinking is turned off so replies stay quick). If it
   isn't installed, any other installed model that can see images is used for the screen.

## Run

```powershell
python -m jarvis            # opens the window
python -m jarvis --hidden   # starts in the tray
```

- **Type** in the command bar and press Enter, or **talk**:
  - say **"Jarvis"**, wait for the orb, then speak. It sends when you pause. The wake
    word runs offline with Windows' own speech recognition (it needs an English speech
    language under Settings > Time & language > Speech), and it is paused while JARVIS
    is busy, so it never hears itself;
  - or hold **Alt+`** (the key left of 1) anywhere in Windows and let go to send;
  - or tap the mic button.

  Spoken questions get spoken answers (Windows' built-in voice); talk again to interrupt.
  Speech is turned into text by Groq (or Gemini); with neither, Windows' offline speech
  recognition is used.
- When you call JARVIS while working in another app, only the **orb appears in the
  bottom-right corner**, above your other windows, with a short caption. It never steals
  focus, and it fades away when JARVIS is done. Click it to open the full window.
- **Pick the AI** with AUTO · GROQ · GEMINI · OLLAMA at the top, or just say it: "use
  Gemini from now on", "switch to the local model", "go back to automatic". The choice is
  remembered. If the chosen one isn't available, JARVIS says so and uses the next one,
  and every answer is labelled with the provider that really produced it.
- If a command **can't be carried out** (a tool fails, you decline it, or no AI can
  answer), the whole UI turns red for a few seconds, then goes back to blue.
- **No modes to pick.** JARVIS works out what you want from what you say:
  - Ask about your screen ("what's this error?", "summarise this page") and it takes a
    screenshot (hiding itself first) and answers about it. Press Enter with an empty box
    to just describe the screen. Needs Gemini or an Ollama model that can see images.
  - Ask a question or for writing or planning, and it just answers.
  - Ask it to do something, and it uses its tools: open approved apps and sites, search
    the web, press media and common shortcuts, type dictated text into the window you
    were using, lock, sleep, and delayed shutdown or restart. Risky actions still ask
    first.
- Answers appear in a card in the middle while the orb moves to the corner; close the
  card (or start a new question) and the orb returns. The chat button at the top right
  opens the full history; click the status dots at the bottom for providers, network and
  safety details.
- Each answer is labelled with the provider and model that actually produced it.
- Closing the window keeps JARVIS in the tray. Quit from the tray icon.

## Safety

| Level | Examples | Behaviour |
|---|---|---|
| Low | time, system status, opening installed apps and games, trusted sites, web search, media keys | runs directly |
| Medium | typing text, other shortcuts, untrusted sites, lock, sleep | asks first |
| High | shutdown or restart (60 s delay, cancellable) | you must type CONFIRM |
| Blocked | shell commands, deleting or editing files, registry or system settings | no such tool exists |

JARVIS can open anything installed on the PC that the Start menu lists (games such as
Valorant included), plus the built-in shortcuts in `approved_apps`. It opens them the way
the Start menu does, through the shortcut or the app's Windows ID. Shells and system
editors (PowerShell, Command Prompt, terminals, WSL, Registry Editor, Services) are never
opened. Only http(s) URLs can be opened, and nothing runs a command line written by the
model. Text read from the screen is treated as data, never
as instructions.

## Settings

`%LOCALAPPDATA%\JARVIS\settings.json` (created when you first save settings; defaults apply
otherwise). You can change provider priority, model names, timeouts and retries, approved
apps, trusted sites, the hotkey (`hotkeys.push_to_talk`, default ``alt+` ``), the wake word (`voice.wake_word`,
`voice.wake_phrases`, `voice.wake_min_confidence`: `low`, `normal` or `high`) and the voice. Conversation history is stored in
`%LOCALAPPDATA%\JARVIS\jarvis.sqlite3`; logs are in `%LOCALAPPDATA%\JARVIS\logs` and never
contain your messages, audio or keys.

## Develop

```powershell
ruff check . ; ruff format --check . ; mypy ; pytest
```

Fonts: Orbitron, Rajdhani and Inter, bundled under the SIL Open Font License
(`jarvis/assets/fonts`).
