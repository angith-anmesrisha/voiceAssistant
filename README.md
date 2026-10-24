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
   `ollama pull qwen3.5:4b`. JARVIS uses `qwen3.5:4b` for chat and, since it can see
   images, for screen questions too (thinking is turned off so replies stay quick). It is
   several times faster than `qwen3.5:latest` (the 9B, about 7 GB) on a laptop without a
   graphics card; if only `qwen3.5:latest` is installed, that one is used. If no model that
   can see is installed, any other installed model that can see images is used for the
   screen. If local replies stop mid-sentence, give Ollama more room: set the user
   environment variable `OLLAMA_CONTEXT_LENGTH` to `8192` and restart Ollama. To use
   another local model, put e.g. this in `%LOCALAPPDATA%\JARVIS\settings.json`:
   `{"providers": {"ollama": {"chat_model": "qwen3.5:latest", "vision_model": "qwen3.5:latest"}}}`

## Run

```powershell
python -m jarvis            # opens the window
python -m jarvis --hidden   # starts in the tray
```

- **Type** in the command bar and press Enter, or **talk**:
  - say **"Jarvis"**, wait for the orb, then speak. It sends when you pause. After it
    answers it **keeps listening**, so just say your next command, no wake word needed.
    After 30 s of silence (`voice.follow_up_s`) it goes back to waiting for "Jarvis";
    typing also ends the conversation. The wake word runs offline with Windows' own
    speech recognition (it needs an English speech language under Settings > Time &
    language > Speech), and it is paused while JARVIS is busy, so it never hears itself;
  - or hold **Alt+`** (the key left of 1) anywhere in Windows and let go to send;
  - or tap the mic button.

  Spoken questions get spoken answers; talk again to interrupt.
  Speech is turned into text by Groq (or Gemini); with neither, Windows' offline speech
  recognition is used.
- When you call JARVIS while working in another app, only the **orb appears in the
  bottom-right corner**, above your other windows, with a short caption. It never steals
  focus, and it fades away when JARVIS is done. Click it to open the full window.
- **Pick the AI** with AUTO · GROQ · GEMINI · OLLAMA at the top, or just say it: "use
  Gemini from now on", "switch to the local model", "go back to automatic". The choice is
  remembered. If the chosen one isn't available, JARVIS says so and uses the next one,
  and every answer is labelled with the provider that really produced it.
- **Automatic fallback:** Groq, then Gemini, then your local `qwen3.5:4b`. Inside
  Groq, a rate-limited qwen hands over to GPT-OSS 120B, then Llama 3.3 70B; Gemini uses
  3.8 Flash, then 3.5 Flash-Lite, 3.6 Flash and 2.5 Flash. A timeout, a rejected key or a
  billing problem moves on to the next service at once. While it works, the caption says
  who it's asking ("Groq didn't answer, asking Gemini…"), and a note says why a backup
  answered.
- **Something not answering?** Say or type "test the AI providers", or press TEST AI
  PROVIDERS in the details panel. Each AI gets a one-word request (each of its models in
  turn, until one answers; that one is used from then on), and JARVIS tells you
  exactly what each one said back and whether the problem is on your account (key, quota,
  billing), on their servers, your network, or JARVIS.
- If a command **can't be carried out** (a tool fails, you decline it, JARVIS says it
  can't, or no AI can answer), the whole UI turns red for a few seconds, including the
  corner orb and its caption, then goes back to blue.
- **Context aware.** Each request includes the window you're working in, your other open
  windows and what JARVIS did recently, so "close this", "copy it to my desktop" or
  "undo that" work without spelling everything out.
- **No modes to pick.** JARVIS works out what you want from what you say:
  - Ask about your screen ("what's this error?", "summarise this page") and it takes a
    screenshot (hiding itself first) and answers about it. Press Enter with an empty box
    to just describe the screen. Needs Gemini or an Ollama model that can see images.
  - Ask a question or for writing or planning, and it just answers.
  - Ask it to do something, and it uses its tools: open apps, games and sites, search
    the web, create, find, copy, move, rename and delete (to the Recycle Bin) files and
    folders, copy and paste, about 80 named keyboard shortcuts (media, Windows, editing,
    browser) plus any other key combination, type dictated text into the window you
    were using, lock, sleep, and delayed shutdown or restart. Risky actions still ask
    first.
- **Long requests become a task list.** Ask for several things at once ("open Spotify
  and play something relaxing, then make a folder called Trip on my desktop, and after
  that tell me a fun fact about Paris") and JARVIS first splits it into tasks, shows them
  in the card, then works through them one by one, ticking each off (✓ done, ✱ working,
  ✕ didn't work). Risky steps still ask first. If one task fails the others still run,
  and the summary at the end (and the red tint) says which one didn't work. Short
  requests skip the planning step.

![JARVIS working through a task list](docs/renders/16-plan-running.png)

- **Remembers you.** Tell it "my sister is Ayşe" or "I live in Kochi" and it keeps that
  on your PC (`keep.sqlite3`), uses it later, and forgets it when you ask.
- **Reminders and timers.** "Remind me at 6 pm to call Mom", "set a 10 minute timer":
  it says them out loud when due, even after a restart.
- **Weather, news and web answers.** "Will it rain tomorrow?", "latest news on the
  election", "what's the price of a PS5?": it reads the results and answers, saying
  where it came from. "Keep an eye on news about SpaceX" checks once a day.
- **Undo.** "Undo that" takes back JARVIS's own last file move, rename, copy, new folder
  or file, desktop tidy-up, or volume, brightness or wallpaper change.
- **Documents and your PC.** "Summarise the PDF in my Downloads", "save these notes to
  my desktop", "tidy up my desktop", "set the volume to 30", "dim the screen", "make
  space.png my wallpaper", "close Spotify". If an app is already open it is brought to
  the front instead of opened twice, and JARVIS only says an app opened once its
  window has actually appeared.
- **Works inside apps.** Ask "send Mom a WhatsApp message saying I'll be late" and
  JARVIS opens WhatsApp, reads its window (buttons, search box, chats, message box) with
  Windows' accessibility interface, then clicks and types step by step, checking the
  window after each step, until it's done. It asks before anything that sends, pays,
  deletes or calls, and before pressing Enter to send. It never touches shells,
  terminals, Windows Settings, Task Manager or password fields, and text it reads in
  apps is checked for hidden instructions like everything else.
- Answers appear in a card in the middle while the orb moves to the corner; close the
  card (or start a new question) and the orb returns. The chat button at the top right
  opens the full history; click the status dots at the bottom for providers, network and
  safety details.
- Each answer is labelled with the provider and model that actually produced it.
- Closing the window keeps JARVIS in the tray. Quit from the tray icon.

## Safety

| Level | Examples | Behaviour |
|---|---|---|
| Low | reading an app's window, clicking and typing to move around in it, time, system status, opening installed apps and games, trusted sites, web search, media keys, copy, listing, finding and opening files, creating folders, the clipboard | runs directly |
| Medium | sending a message, clicking Send/Pay/Delete/Call in an app, typing text, paste, undo, other key combinations, copying, moving or renaming files, deleting a file, tidying the desktop, untrusted sites, lock, sleep | asks out loud: say yes or no (or click) |
| High | deleting a folder, shutdown or restart (60 s delay, cancellable) | you must type CONFIRM |
| Blocked | shell commands, running programs or scripts from files, system folders, the registry and system settings, Win+R, Win+X, Ctrl+Alt+Del | no such tool exists, or refused |

Text that comes from outside (clipboard, folder listings, the screen description,
window titles) is checked by Meta's **Llama Prompt Guard 2** on Groq before the chat
model sees it. If it looks like an attempt to give JARVIS orders, it is withheld and the
UI turns red. (Prompt Guard is a classifier, so it doesn't answer; qwen still does.)

File tools only work inside your own folders (your user folder and any drive that isn't
the Windows drive), never in Windows, Program Files, ProgramData or AppData, and links
are resolved first so they can't point elsewhere. Nothing is deleted outright: it goes to
the Recycle Bin, and your main folders (Desktop, Documents, Downloads...) can't be removed.
Copy and move never overwrite. Opening a file uses its default app and never runs `.exe`,
scripts, installers or shortcuts.

JARVIS can open anything installed on the PC that the Start menu lists (games such as
Valorant included), plus the built-in shortcuts in `approved_apps`. It opens them the way
the Start menu does, through the shortcut or the app's Windows ID. Shells and system
editors (PowerShell, Command Prompt, terminals, WSL, Registry Editor, Services) are never
opened. Only http(s) URLs can be opened, and nothing runs a command line written by the
model. Text read from the screen is treated as data, never
as instructions.

## JARVIS's voice and manner

JARVIS speaks with a refined British male voice that runs **on your PC**: free, offline
and unlimited, with no account or API key. It uses the open Kokoro speech model (Apache
2.0). The first start downloads the model once (about 350 MB, into
`%LOCALAPPDATA%\JARVIS\voices`, checked against a fixed checksum); Windows' own voice
speaks until it's ready, and takes over again if the model ever can't run.

It is a stock voice chosen to sound like the film's composed English AI, not a copy of
the actor's voice (cloning a real person's voice without permission isn't something
JARVIS does). Four British male voices are included; set `voice.neural_voice` to try them:

| Voice | Notes |
|---|---|
| `bm_george` (default) | the best-rated British male voice in Kokoro's own list |
| `bm_fable` | also rated well; worth comparing with George |
| `bm_lewis`, `bm_daniel` | rated lower by Kokoro's authors, but some prefer them |

`voice.neural_speed` sets the pace (0.95 by default, measured; 0.7 to 1.3). Set
`voice.tts_engine` to `"sapi"` to use only Windows' voice, or `voice.download_voice` to
`false` to never download. A short pause separates sentences, and Markdown, links, code,
emoji and file paths are never read aloud (a link becomes the site's name).

Replies, task reports and notices follow one personality: composed, concise British
English that leads with the result, with the odd dry remark, and it never claims to have
done something its tools didn't actually do.

## Settings

`%LOCALAPPDATA%\JARVIS\settings.json` (created when you first save settings; defaults apply
otherwise). You can change provider priority, model names, timeouts and retries, approved
apps, trusted sites, the hotkey (`hotkeys.push_to_talk`, default ``alt+` ``), the wake word (`voice.wake_word`,
`voice.wake_phrases`, `voice.wake_min_confidence`: `low`, `normal` or `high`), how long it keeps listening for
follow-ups (`voice.follow_up_s`, 0 to turn off) and the voice (see above). Conversation history is stored in
`%LOCALAPPDATA%\JARVIS\jarvis.sqlite3`; logs are in `%LOCALAPPDATA%\JARVIS\logs` and never
contain your messages, audio or keys.

## Develop

```powershell
ruff check . ; ruff format --check . ; mypy ; pytest
```

Fonts: Orbitron, Rajdhani and Inter, bundled under the SIL Open Font License
(`jarvis/assets/fonts`).
