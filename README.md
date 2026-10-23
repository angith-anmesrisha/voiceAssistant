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

3. Add API keys as **environment variables** (they are never written to files):

   ```powershell
   setx GROQ_API_KEY "gsk_..."      # https://console.groq.com/keys  (primary)
   setx GEMINI_API_KEY "AIza..."    # https://aistudio.google.com/apikey  (backup, screen analysis)
   ```

   Close and reopen the terminal after `setx`. You can use either key, or both.

4. Optional, for offline use: install [Ollama](https://ollama.com) and run
   `ollama pull llama3.2`.

## Run

```powershell
python -m jarvis            # opens the window
python -m jarvis --hidden   # starts in the tray
```

- **Type** in the command bar and press Enter, or **talk**: tap the mic button, or hold
  **Ctrl+Alt+Space** anywhere in Windows and release to send. Spoken questions get spoken
  answers (Windows' built-in voice); tap the mic to interrupt.
- **Modes**
  - **Analyze** looks at your screen (JARVIS hides itself for the screenshot) and answers
    about it. Press Enter with an empty box to just analyze. Needs Gemini (or an Ollama
    vision model set in settings).
  - **Assist** is chat: answers, writing, planning. It never touches your PC.
  - **Execute** lets JARVIS use its tools: open approved apps and sites, search the web,
    press media and common shortcuts, type dictated text into the window you were using,
    lock, sleep, and delayed shutdown or restart.
- Each answer is labelled with the provider and model that actually produced it.
- Closing the window keeps JARVIS in the tray. Quit from the tray icon.

## Safety

| Level | Examples | Behaviour |
|---|---|---|
| Low | time, system status, approved apps, trusted sites, web search, media keys | runs directly |
| Medium | typing text, other shortcuts, untrusted sites, lock, sleep | asks first |
| High | shutdown or restart (60 s delay, cancellable) | you must type CONFIRM |
| Blocked | shell commands, deleting or editing files, registry or system settings | no such tool exists |

Only programs in `approved_apps` can be launched, only http(s) URLs can be opened, and nothing
runs a command line written by the model. Text read from the screen is treated as data, never
as instructions.

## Settings

`%LOCALAPPDATA%\JARVIS\settings.json` (created when you first save settings; defaults apply
otherwise). You can change provider priority, model names, timeouts and retries, approved
apps, trusted sites, the hotkey and the voice. Conversation history is stored in
`%LOCALAPPDATA%\JARVIS\jarvis.sqlite3`; logs are in `%LOCALAPPDATA%\JARVIS\logs` and never
contain your messages, audio or keys.

## Develop

```powershell
ruff check . ; ruff format --check . ; mypy ; pytest
```

Fonts: Orbitron, Rajdhani and Inter, bundled under the SIL Open Font License
(`jarvis/assets/fonts`).
