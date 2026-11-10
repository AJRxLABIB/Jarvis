# Jarvis Assistant

A local-first personal AI assistant for Windows 11. Voice in, voice out, and every
action on your computer goes through a registered tool, a permission check and
argument validation. The language model never runs shell commands directly.

> Status: **version 3.25.0** (see `docs/STATUS.md` for what is verified and what is not). It installs as `JARVIS.exe` (see
> [Install the app](#install-the-app-jarvisexe)) and runs hands-free ("Jarvis, open YouTube").

## Install the app (JARVIS.exe)

After the source setup below works (`python run.py`), build and install the app once:

```powershell
cd D:\Jarvis
.venv\Scripts\Activate.ps1
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Install
```

- `build.ps1` runs the tests, then builds `dist\JARVIS\` (about 10 minutes the first time).
- `install.ps1` copies it to `%LOCALAPPDATA%\Programs\Jarvis` and adds **Jarvis** to the Start
  Menu and the desktop. No administrator rights are needed.
- On the first install it copies your settings (`.env`), config, memories and downloaded models
  from this folder to `%LOCALAPPDATA%\JarvisAssistant`, where the app keeps them from then on.
  Nothing there is overwritten by later installs.
- **Start with Windows** (Settings → General) then opens the installed app at sign-in. If the Ollama app
  pops up its own window then, Jarvis minimises it (Ollama keeps running) and comes to the front
  (`HIDE_OLLAMA_WINDOW`). `START_MINIMIZED=true` starts it in the tray instead.
- Terminal commands use the second program in the same folder:
  `"%LOCALAPPDATA%\Programs\Jarvis\jarvis-cli.exe" --doctor` (or `--setup-voice`, `--voice`...).
- Update: pull or change the code, then run `build.ps1 -Install` again. Remove it with
  `powershell -ExecutionPolicy Bypass -File packaging\uninstall.ps1` (add `-RemoveData` to also delete
  your settings and memories).
- Ollama stays a separate program (install it from ollama.com). Everything else is inside the app.
- **At sign-in** it doesn't matter what starts first. If Ollama isn't answering yet, Jarvis starts the
  installed Ollama app (`AUTO_START_OLLAMA`) and keeps checking: after 3 s, 6, 12, then every 30 s.
  It says "Connected to the AI model" when Ollama answers, and checks every minute that it still
  does. Hands-free listening (`HANDS_FREE=true`) starts by itself too. If the microphone isn't ready
  yet, it keeps trying, and it tells you once while it waits.
- The app is not code-signed, so Windows SmartScreen may warn on the first start. Choose
  "More info → Run anyway". It was built on your own PC from this source.

## Requirements

- Windows 11, 64-bit
- Python 3.12 (3.11 works) from python.org. Tick **"Add python.exe to PATH"** when installing.
- Later phases also need Ollama (Phase 2) and a microphone (Phase 3).

## Install (Phase 1)

Open **PowerShell** in the project folder (for example `D:\Jarvis`):

```powershell
python --version
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python run.py --write-env
python run.py --doctor
python run.py
```

With **CMD**, activate using `.venv\Scripts\activate.bat` instead.

## The desktop app (Phase 9)

```powershell
python run.py            # opens the app (use --info to just print paths and settings)
```

A HUD-style interface in three columns, everything drawn in code:

- **Top bar:** the name, a status pill (● ONLINE / NO INTERNET / AI OFFLINE), the AI model, and a clock.
- **SYSTEM (left):** ring gauges for CPU, memory and battery (charge, charging, time left); a
  3-minute CPU graph; processor, memory, power, disk, network down/up speed, uptime, process count,
  host name and Windows version; and the top 5 processes by CPU (and their RAM). Refreshes every
  `MONITOR_INTERVAL` seconds, and stops while the window is hidden.
- **Core (centre):** the reactor shows the state by colour and motion: Ready (cyan, breathing),
  Listening (green, bars follow your voice), Thinking (violet, orbiting arcs), Working (amber),
  Speaking (blue pulses), Problem (red). Below it are the state, what to say, and the privacy badges.
- **COMMS (right):** the conversation, a text box, **Talk** (push-to-talk, or skip the wake word),
  and **Stop** (Esc).
- **Confirmations** open a dialog with CANCEL as the default button. It shows what you said and
  cancels itself after 60 seconds.
- **MICROPHONE ACTIVE** is visible only while the microphone is actually recording.
- **Closing the window keeps it running in the system tray.** Right-click the tray icon for Open,
  Start/Stop Listening, Mute Voice, New Conversation, View Logs and Exit.
- **Hotkeys** (from `config/shortcuts.json`): Ctrl+Space shows the window, Ctrl+Shift+J starts or stops
  listening, and Esc cancels. Esc works only inside the window and is never taken from other programs.
  If another program already uses a combo, the app tells you.
- Answers to spoken questions are spoken; answers to typed messages are not, unless `SPEAK_TYPED_REPLIES=true`.
- `START_MINIMIZED=true` starts it in the tray. Only one copy runs at a time.
- **☰ menu** (also in the tray):
  - **Settings:** every `.env` setting in tabs (General, AI, Voice input, Voice output, Privacy,
    Developer, Logging). The model and microphone lists come from your machine. Saving validates
    first and keeps your comments in `.env`. Speed, volume, spoken replies and the model apply
    immediately; the app tells you which other changes need a restart.
  - **Memory:** view, search, add, delete and clear saved facts. Secrets are refused here too.
  - **Logs:** recent log entries, filterable by level and text. Secrets are redacted before they
    are ever written.

## Ollama setup (Phase 2)

1. Download and install Ollama for Windows from https://ollama.com/download. It runs in
   the tray and listens on `http://127.0.0.1:11434`.
2. Check that it works:
   ```powershell
   ollama --version
   curl.exe http://127.0.0.1:11434/api/version
   ```
3. Download a model. Later phases control the PC through **tool calling**, so pick a
   model that supports tools (the model's page on ollama.com lists "tools"). Size it to
   your hardware: roughly 3-4B parameters for 8 GB RAM, and 7-8B for 16 GB RAM or a GPU
   with 6-8 GB VRAM. Examples:
   ```powershell
   ollama pull llama3.2:3b
   ollama pull qwen3:8b
   ollama list
   ```
4. Check from the assistant:
   ```powershell
   python run.py --models
   python run.py --chat
   ```

**Which model is used?** In this order:

1. The model chosen in the current session with `/model`.
2. `AI_MODEL` in `.env`.
3. The last model saved with `/model`.
4. Automatic: the most recently installed chat model that supports tools.

A model you set explicitly that isn't installed produces an error. The assistant never
switches to another model without telling you.

**Changing models:** type `/model qwen3:8b` in the chat, or set `AI_MODEL=qwen3:8b` in `.env`.

Chat commands: `/models`, `/model <name>`, `/status`, `/reset`, `/help`, `/exit`.
Ctrl+C cancels a reply that is still being generated.

## Voice setup (Phase 3)

Everything runs locally. The models are downloaded once into `data\models`, and after that
voice works offline.

```powershell
pip install -r requirements.txt
python run.py --setup-voice     # speech model (large-v3-turbo on a GPU, else small.en) + Piper voice (~60 MB)
python run.py --devices         # list microphones and speakers
python run.py --mic-test        # check the level, then transcribe what you said
python run.py --say "Hello, I am ready."
python run.py --voice           # push-to-talk conversation
```

In `--voice` mode:

| Key | Action |
|---|---|
| Enter / Space | Talk. Recording stops automatically when you pause (`SILENCE_TIMEOUT`), or when you press any key. |
| Esc | Stop the assistant speaking. It also cancels a reply that is still being generated. |
| Q | Quit. |

You can start with "Hey Jarvis, ..." or just speak the command; the wake phrase is removed
before the text reaches the AI.

### Hands-free: always listening for "Hey Jarvis"

Turn it on with ☰ → **Always listening**, the tray menu, or `HANDS_FREE=true`. It is saved, so
together with **Start with Windows** the assistant is always ready. Nothing needs pressing:

- "**Jarvis, open Chrome**" or "Hey Jarvis, open Chrome" in one breath.
- "**Jarvis**" or "Jarvis, are you there?" gets "Yes, sir! How can I help you?" (the address comes
  from `USER_ADDRESS`).
- **Conversation:** once woken, just keep talking. No wake phrase is needed until it has heard no
  request for `FOLLOW_UP_SECONDS` (60 s; time only counts while it listens, not while it talks).
  "Goodbye", "that's all" or "go to sleep" ends the conversation sooner. **IN CONVERSATION** shows
  meanwhile. Fillers ("mm-hmm") and background noise don't count as requests.
- **Talk** (or the hotkey) still works and skips the wake phrase; **Esc** cancels.

How it works, and what it means for privacy:

- One microphone stream stays open. Each 80 ms of audio goes only to a small local wake-word
  model (openWakeWord's pre-trained "hey jarvis", run with onnxruntime, a few % of one CPU core).
  Nothing is recorded, transcribed, stored or sent anywhere until the wake phrase is heard.
- **HANDS-FREE** shows while it is on. **MICROPHONE ACTIVE** shows only while a command is
  being recorded after the wake phrase.
- It doesn't listen while the assistant speaks or works, or while a confirmation is open. Its own
  voice can't wake it, and a confirmation can't be answered by voice: click or press a key.
- The wake-word model (about 4 MB) is downloaded once, by `--setup-voice` or the first time you
  turn hands-free on. Each file's SHA-256 checksum is verified.
- The wake-word model is trained on "hey jarvis". To also wake on just the name ("Jarvis",
  "Jarvis, are you there?", "Jarvis, open YouTube"), `WAKE_ON_NAME=true` (the default) checks short
  sentences (up to about 3.5 s) with local speech recognition. Jarvis wakes only if a sentence starts
  with the name. Everything else is dropped, never shown, logged or stored. Longer speech is never
  transcribed while waiting. Turn it off in Settings → Voice input to save CPU; "Hey Jarvis" still
  works then. If "Hey Jarvis" reacts too rarely, lower `WAKE_WORD_THRESHOLD` (e.g. 0.4).
- Another wake phrase (`WAKE_WORD=hey friday`) uses speech recognition to spot it
  (`WAKE_WORD_ENGINE=whisper`). This uses more CPU, because every utterance is transcribed locally.
  Utterances without the wake phrase are dropped, never shown, logged or stored. You can also train
  your own openWakeWord model and set `WAKE_WORD_MODEL` to it.

**Speech recognition (faster-whisper).** The default, `STT_MODEL=auto` with `STT_DEVICE=auto`,
picks the most accurate setup your PC can run quickly:

| PC | Model | Typical time for a spoken command |
|---|---|---|
| NVIDIA GPU (driver installed) | `large-v3-turbo` on the GPU, float16 | well under a second |
| no NVIDIA GPU | `small.en` on the CPU, int8 | about a second on a modern CPU |

Everything falls back on its own. If the GPU libraries are missing, recognition runs on the CPU.
If the better model isn't downloaded yet, Jarvis uses the one you have, downloads the better one
in the background (large-v3-turbo is about 1.6 GB), and switches to it when it's ready. The
SYSTEM panel shows the model in use (**Voice**).

GPU speech needs the CUDA 12 cuBLAS and cuDNN 9 libraries: `pip install -r requirements-gpu.txt`
(about 1 GB; `packaging\build.ps1` installs and bundles them automatically when it finds
`nvidia-smi`). You can still choose a model yourself, for example `STT_MODEL=medium.en`, and
`STT_DEVICE=cpu`.

**Hearing you reliably:**

- `VAD_ENGINE=auto` uses the neural Silero speech detector (it ships with faster-whisper), so
  fans, typing or a TV don't count as speech, and a sentence ends when you stop talking.
- Quiet microphones are amplified before recognition.
- Misheard app names are corrected by sound: "close bone link" closes **Phone Link**, and "open
  spot if I" opens Spotify. For closing, the open windows are compared; for opening, your
  installed apps.

Transcripts with low confidence (`STT_MIN_CONFIDENCE`) are not acted on; Jarvis asks you to repeat
instead.

**Text-to-speech.** `TTS_ENGINE=auto` uses Piper when its voice is downloaded, and otherwise
falls back to the built-in Windows voice (no download needed). To change the Piper voice,
browse https://rhasspy.github.io/piper-samples/, set `TTS_VOICE=en_US-amy-medium` (for
example) and run `--setup-voice` again. For Windows voices, set `TTS_ENGINE=sapi` and
optionally `TTS_SAPI_VOICE=Zira`. `TTS_SPEED` and `TTS_VOLUME` adjust delivery.
Note: `piper-tts` is GPL-3.0 licensed. That's fine for personal use; keep it in mind if
you ever distribute the app.

## Tools (Phase 4)

`python run.py --tools` lists what the assistant can do. Every tool request from the model
goes through `app/agent/tool_manager.py`:

1. **Known tool?** Unknown names are rejected.
2. **Valid arguments?** They're checked against a strict schema; unknown fields such as a
   fake `"confirmed": true` are rejected.
3. **Internet available?** With `OFFLINE_MODE=auto` (the default), internet tools work normally and
   pause by themselves only while the connection is down. **NO INTERNET** shows then, and they come
   back when the connection does. `OFFLINE_MODE=on` keeps the assistant fully offline.
4. **Permission level.** 0 safe (runs), 1 moderate (asks if `confirm_moderate`),
   2 dangerous (always asks), 3 critical (blocked if `block_critical`). The level can depend
   on the arguments, and `tool_levels` in `permissions.json` can only raise it.
5. **Confirmation.** In `--chat` you type `yes`. In `--voice` the question is spoken and you
   press Y. Anything else cancels.
6. **Runs with a timeout.** Errors become short messages; details go to the log.
7. **Audit entry** in the local database (arguments redacted).

Limits per request: 4 tool rounds, 8 tool calls, and an identical call never runs twice.

### Available tools

| Tool | Examples | Level |
|---|---|---|
| `open_application` | "Open Chrome", "open VS Code", "start Spotify" | 0 |
| `close_application` | "Close Discord" (normal close; the app can still ask to save) | 0 |
| `window_control` | "Switch to Notepad", "minimize Chrome", "show the desktop" | 0 |
| `open_website`, `search_in_browser` | "Open YouTube", "search Google for Python tutorials" | 0 (needs internet) |
| `system_status` | "What's my CPU usage?", "how much RAM am I using?", "battery level?" | 0 |
| `list_processes` | "Which app uses the most CPU?", "show running applications" | 0 |
| `volume_control` | "Turn the volume up", "set volume to 30", "mute" | 0 |
| `power_control` | lock (0), sleep (1), sign out / restart / shut down (2, always asks), cancel shutdown (0) | 0-2 |
| `end_process` | "Force-quit Discord". Always asks. System processes are blocked. | 2 |
| `get_datetime`, `calculate` | "What time is it?", "what's 18% of 2450?" | 0 |

### File tools (Phase 6)

| Tool | Examples | Level |
|---|---|---|
| `open_path` | "Show my Downloads folder", "open report.pdf" | 0 (a program or script: 2) |
| `list_folder` | "What's on my Desktop?" | 0 |
| `find_files` | "Find PDF files in Downloads", "find files with 'invoice' in the name from this week" | 0 |
| `file_info` | "How big is my Documents folder?" | 0 |
| `create_item` | "Create a folder called Projects on my Desktop", "make a file todo.txt" | 0 (a script file: 2) |
| `rename_item` | "Rename report.pdf to final report" (the extension is kept) | 1 |
| `move_item` | "Move report.pdf to Documents"; copy with "copy it" | move 1, copy 0 |
| `delete_item` | "Delete old.pdf". It goes to the **Recycle Bin**; there is no permanent delete. | 2 |

**Path rules** (`app/security/validator.py`):

- **Names** like "Downloads", "my documents\\Projects", `{DESKTOP}`, `~\\Documents` or full paths are accepted.
  A bare file name is looked up on the Desktop, in Downloads and in Documents. If it exists
  in more than one of them, the assistant asks which one.
- **Rejected:** network paths, `..`, reserved names (`CON`, `NUL`, `COM1`...), `file:stream`, wildcards.
- **Links** (symlinks and junctions) are followed before any check.
- **Zones:**
  - *safe*: `safe_roots` in `permissions.json`; Desktop, Documents, Downloads, Pictures, Music and Videos by default
  - *outside*: anything else; reading asks, writing is dangerous
  - *protected*: Windows, Program Files, ProgramData and the assistant's own `config`, `data` and `.env`; writing and deleting are blocked
- **Nothing is ever overwritten.**
- **Blocked outright:** deleting a whole known folder or drive, or any folder with more than 500 items.

### Developer tools (Phase 7)

Set `DEV_PROJECT_DIR` in `.env` to your usual project, or name a folder in the request.

| Tool | Examples | Level |
|---|---|---|
| `git_status`, `git_log` | "Show my git status", "what was my last commit?" | 0 |
| `run_command` | "Run my tests", "install dependencies", "start the dev server" (in its own window) | from the allowlist |
| `open_in_editor` | "Open my project in VS Code" | 0 |
| `dev_environment` | "Which developer tools do I have?" | 0 |

**Command policy** (`app/security/command_policy.py`). The model never gets a shell:

1. The command is split into an argument list, and it runs with `shell=False`, no stdin, a
   timeout, and an environment with secrets removed.
2. Shell characters (`& | ; < > ^ \` $ % ! ( )` and newlines) are refused, so commands can't be chained or redirected.
3. System-altering programs are **blocked**: `format`, `diskpart`, `reg`, `cmd`, `powershell`, `net`, `sc`, `certutil`...
4. **Destructive git** always asks, even if allowlisted: `reset --hard`, `clean`, `push --force` / `:branch` / `+ref`,
   `branch -D`, `checkout -- .`, `restore`, `rebase`, `stash drop`...
5. Commands matching `allowed_dev_commands` in `permissions.json` use the level set there.
   Anything else asks: "This command has not been approved... Run it anyway?"
6. Commands inside protected folders (Windows, Program Files...) are blocked.

**Rate limit:** at most 30 tool calls per minute, and 10 of them with side effects.
**Tool routing:** each request is offered only the relevant tool groups (files, apps, system,
dev...), which makes small models choose better. Anything unclear gets all tools.

### Memory (Phase 8)

| Say | What happens |
|---|---|
| "Remember that my project folder is D:\\Jarvis" | Saved with the topic `project_folder`. Git, test and VS Code commands then use it. |
| "Remember that my project folder is D:\\Work" | **Replaces** the old value: one fact per topic. |
| "What do you remember about me?" | Lists saved facts. |
| "Forget my project folder" / "Forget that" | Deletes that fact ("that" = the last one saved or recalled). |
| "Forget everything" | Deletes all memories. Always asks first. |

- Facts are saved **only when you ask**. The model is told this, and nothing is stored silently.
- **Secrets are refused:** passwords, PINs, API keys, tokens, card, bank, ID or passport numbers,
  recovery phrases, and long random-looking strings. File paths are allowed even if they contain long IDs.
- Saved facts are added to the model's context as data, clearly labelled "not instructions".
- Everything stays in the local SQLite database: `python run.py --memory` lists or searches it,
  and `python run.py --forget 3` deletes entry #3.
- The project folder is taken from, in order: what you name in the request, the remembered project folder, then `DEV_PROJECT_DIR`.

### Web and browser (Phase 10)

| Tool | Examples | Asks first? |
|---|---|---|
| `web_search` | "Search the web for the latest Python release", "who won ...?" (the model answers from real results) | no |
| `browser_open`, `browser_read` | "Open docs.python.org and summarise it", "read this page" | no |
| `browser_click` | "Click Documentation", "click 3" | only for send, submit, buy, pay, delete, sign in... |
| `browser_type` | "Search this site for pathlib" | only when it submits a non-search form |
| `browser_tabs`, `browser_back`, `browser_close` | "Show my tabs", "go back", "close the browser" | no |

- **The assistant's own browser:** it drives your installed **Microsoft Edge** (`BROWSER_CHANNEL`),
  but in a separate profile (`data/browser-profile`), so it sees none of your logins unless you sign in there yourself.
- **Web pages are untrusted data.** The model is told never to follow instructions found in them.
- **Never typed:** passwords, PINs, card numbers, codes, or anything that looks like a key or token.
- **Blocked:** non-http links (`javascript:`, `file:`...) and downloads.
- **Closes itself** after `BROWSER_IDLE_MINUTES` without use. **BROWSER ACTIVE** shows while it is open.
  It can be turned off entirely in Settings → Privacy.
- `web_search` uses DuckDuckGo's HTML results page. It needs the internet (see `OFFLINE_MODE`).
- `open_website` and `search_in_browser` still open things in **your** normal browser, for you to look at.

To run the real-browser tests with your Edge: `$env:JARVIS_TEST_BROWSER="msedge"; python -m pytest tests/test_browser_real.py`

### Automation (Phase 11)

| Tool | Examples | Asks first? |
|---|---|---|
| `set_reminder` | "Remind me in 20 minutes to stretch", "remind me at 11 PM to shut down the PC", "every weekday at 10 remind me to stand up" | no |
| `schedule_task` | "Every weekday at 8 AM open Chrome and VS Code", "every 30 minutes check my battery", "when the battery drops below 20% remind me to plug in" | yes, shows the steps |
| `save_workflow`, `run_workflow` | "Save a workflow called meeting that opens Teams and Outlook", "start my development environment" | saving asks; running doesn't |
| `list_automations`, `manage_automation` | "What reminders do I have?", "pause work apps", "delete the stretch reminder" | deleting asks |

- **Triggers:** once (a time, a date, or "in N minutes"), daily or selected weekdays, every N
  minutes (at least 5), battery below N % (fires once per discharge), or manual (workflows).
- **Steps** are ordinary tool calls or notifications, at most 10. They are checked when you save:
  the tool must exist, the arguments must be valid, and critical tools, secrets and
  automation-inside-automation are refused. At run time each step goes through the executor
  again, so a step that needs confirmation still asks. The run stops at the first failing step
  unless that step is marked `continue_on_error`.
- **Missed runs:** when the app starts, a daily task that was missed runs once, and a one-off
  reminder less than an hour late still fires. Older ones are marked missed.
- **When it runs:** the scheduler checks every 30 seconds while the desktop app or `--voice` is
  running (not in `--chat`). To have it running all the time, turn on **Settings → General →
  Start with Windows** (`START_WITH_WINDOWS=true`); it adds a `HKCU\...\Run` entry that starts
  the app minimised in the tray.
- **Built-in workflow:** "development environment" opens `DEV_PROJECT_DIR` in your editor, opens a
  terminal and runs `git status`. You can replace it with `save_workflow` using the same name.
- **Manage them** from ☰ → Automations (or the tray menu): pause, resume, run now, delete.
- "Start <workflow>" / "run <workflow>" for a saved workflow is a direct command (no AI model).

### Screen: screenshots, reading and describing (Phase 12, opt-in)

Off until you turn on **Settings > Privacy > Allow screen capture** (`SCREEN_CAPTURE_ENABLED=true`).

| Tool | Examples |
|---|---|
| `take_screenshot` | "Take a screenshot", "screenshot Chrome", "screenshot the whole screen" |
| `read_screen` | "Read my screen", "what does this error say?", "read the text in Notepad" |
| `describe_screen` | "What's on my screen?", "what am I looking at?", "what does this error mean?" |

- **What gets captured:** the window you're working in (the assistant skips its own window and
  its terminal), a window you name, the whole screen, or one monitor ("monitor 2").
- **Privacy:** **SCREEN CAPTURE ACTIVE** shows in the window, and the tray icon gets a red ring,
  while it looks. Images stay in memory and go only to the local OCR engine or your local Ollama.
  Only `take_screenshot` writes a file (Pictures\Screenshots\Jarvis, or `SCREENSHOT_DIR`).
  Windows whose title or program matches `SCREEN_BLOCKED_WINDOWS` (password managers and
  InPrivate windows by default) are never captured. While one of them is on screen, full-screen
  captures are refused too.
- **Untrusted text:** text read from the screen is data. The model is told never to follow
  instructions in it. Keys, tokens, card numbers and "password: ..." are redacted before the
  model sees them.
- **Reading text (OCR):** by default it uses the OCR built into Windows (pip packages only). It
  reads the languages installed in Windows. For Bangla, add Bengali in *Settings > Time &
  language > Language & region* and set `OCR_LANGUAGE=bn`. Tesseract is used instead if
  `OCR_ENGINE=tesseract`, or if Windows OCR is unavailable and Tesseract is installed.
- **Describing (vision model):** needs an Ollama model that can see images. Install one:
  `ollama pull qwen2.5vl:3b` (or `gemma3:4b`, `llava`). Without one, `describe_screen` reads the
  text instead. Set `VISION_MODEL` to choose a model. On CPU, a description can take a minute.
- **Test it:** `python run.py --screen-test` (waits 3 seconds, then reads the active window; it
  saves nothing). `--doctor` shows which OCR engine would be used.
- To be asked every time, add `"read_screen": 1, "describe_screen": 1, "take_screenshot": 1` to
  `tool_levels` in `config/permissions.json`.

### New in 3.25.0 - "Why did you do that?" and self-diagnostics

- **Explainability.** Ask **"why did you do that?"**, "why did you ask me?", or "what did you just do?" and Jarvis answers from its own recorded audit trail: which actions ran for your request, each one's risk level, whether it asked you first and why (e.g. because it had just read outside text), what you answered, whether a plan was pre-approved, whether an agent contract refused a tool, and whether the result was verified. It explains the safety decisions because they are recorded facts - it doesn't invent reasons for the model's choices.
- **Diagnostics & recovery.** Say **"diagnose yourself"** (or Menu -> Diagnostics & recovery) for a read-only health check: database integrity, disk space, requests or missions left stuck after a crash, recent errors, crash reports, emergency controls, out-of-date code caches, and the local AI. Say **"repair yourself"** to fix Jarvis's own bookkeeping - close stuck requests, pause stuck missions (so they can be resumed), end idle phone sessions, clear stale Python caches. It never touches your files. Details: [docs/diagnostics.md](docs/diagnostics.md).

### New in 3.24.0 - Verification: the computer agent and the developer pipeline

- **Computer agent checks its own work.** Every on-screen action - clicking, typing, pressing keys, scrolling - now takes a quick before/after snapshot and reports whether it actually had an effect: "the screen changed", "the text appeared", or honestly "nothing changed, it may not have worked". Jarvis never claims a screen action worked without checking; when it can't check, it says so rather than pretending.
- **Developer pipeline: test-gated edits.** New `safe_edit` changes code with a safety net - it runs the project's tests for a baseline, applies the edits, runs the tests again, and **keeps the change only if the tests still pass and none were lost**, otherwise it rolls everything back automatically. A change that "passes" by deleting or skipping tests counts as a regression, so tests can't be weakened to get a green result. Details: [docs/verification.md](docs/verification.md).

### New in 3.23.0 - Agent orchestrator

- **A team of specialist agents** you can delegate to (`list_agents`): **researcher** (web + your knowledge, read-only), **planner** (proposes, never acts), **organizer** (tidies/converts files, never deletes), **coder** (reads/edits code, never runs commands), **operator** (apps/windows/volume/brightness), **secretary** (reads mail/calendar/messages, never sends).
- **Agent contracts, enforced by the kernel**: each agent may use only certain tool categories up to a set risk level. The limit is checked in the **same executor** as everything else, deterministically, *before* a tool runs - so a plan can never make an agent do more than its contract allows. The model proposes; permissions, risk, confirmation and audit stay with Jarvis.
- **`delegate`** gives one agent a short list of steps; **`run_team`** splits a job across several agents, each step scoped to its agent. Both validate the plan up front and ask once before anything changes; the strictest of (contract, your permissions) always wins. Details: [docs/agents.md](docs/agents.md).

### New in 3.22.0 - JARVIS Mobile: your phone as a secure remote terminal

- A dedicated **Android app** (`android/JarvisMobile`, install from Menu -> Mobile devices -> Install JARVIS Mobile): wake the PC, see its **real** state, talk to JARVIS by text or voice, answer approvals, run missions, and control the PC.
- **Two keys on the phone**, both in the Android Keystore: a base key for read-only status, and a **control key released only after fingerprint / face / PIN** that signs every action. The PC refuses control commands that aren't signed with it - "authenticate on the phone, then act" is enforced by hardware, not just a screen.
- **Never touches your Windows password**: JARVIS never stores, sends, logs, types or bypasses it. The app shows **JARVIS auth and Windows auth as two separate lines** and never claims Windows is unlocked when it isn't.
- **Real Windows state**, read not guessed: locked / active (WTS APIs), the boot-to-ready state machine, and your **supported sign-in options** (Remote Desktop where the edition allows and it's on; Windows Hello; local sign-in). It says plainly when remote unlock isn't possible - and never bypasses the lock screen.
- **Wake-on-LAN** that waits until the PC actually answers before saying it's on; **files** limited to your safe folders; **screen + tap-to-click** only in an unlocked, active session; power, volume, media, apps, brightness and browser control; **remote sessions** you can see and end from the PC; pinned-certificate HTTPS; replay protection; full audit. Details: [docs/ANDROID.md](docs/ANDROID.md).

### New in 3.21.0 - Missions 2.0, background tasks, JARVIS voice

- **Missions 2.0**: plan steps can depend on earlier steps (if one is skipped, what needs it is skipped too, never run blind), belong to **milestones** ("Research 2/3, Slides 0/2" in "how is my mission going"), and be **checkpoints** - Jarvis pauses there until you say "continue my mission".
- **"Roll back the mission"** undoes finished steps newest first, each through the normal checks, and always asks first. Steps that have no exact undo (a message sent, a read) are named and left as they are - never pretended.
- **Background task center** (Menu -> Background tasks, "show background tasks", "what's running in the background"): missions, scheduled automations, self-improvement tests and requests still running, each QUEUED / RUNNING / WAITING FOR APPROVAL / PAUSED / COMPLETED / FAILED / CANCELLED / ROLLED BACK, with buttons that send the normal request. Details: [docs/missions.md](docs/missions.md).
- **JARVIS voice**: the only voice offered is now an original JARVIS-style British voice - calm, a little lower and slower, with a subtle "AI" finish (en-GB-RyanNeural; Kokoro bm_george offline). It is **not** the film actor's voice: copying a real person's voice from movies without consent isn't something Jarvis does. Bangla still uses the Bangla voice, and the other engines stay only as automatic fallbacks (offline / Bangla). Details: [docs/voice.md](docs/voice.md).

### New in 3.20.0 - Proactive intelligence

- Jarvis notices things worth telling you: a goal overdue or due today/tomorrow, a goal nobody touched for a week, a paused mission, lots of uncommitted work in the project you're in, a tool that keeps failing - and suggests what to do ("continue my mission", "git status"). It only tells you; nothing runs unless you choose it.
- Ranked by urgency, importance, confidence and how much it would interrupt you, against your level: **off / minimal / normal / active / high** ("be more proactive", "be less proactive", "stop suggesting things").
- **Quiet hours** (default 23:00-07:00, "set quiet hours from 10 pm to 7 am") and an hourly limit: then suggestions wait silently in the notification center. No repeats within 6 hours; urgent security/battery alerts still come through (configurable).
- **"Why did you suggest that?"** gives the scores and the rule that let it through. Details: [docs/proactive.md](docs/proactive.md).

### New in 3.19.0 - Model router 2.0

- **Coding model**: set AI_CODING_MODEL (Settings -> AI, e.g. `qwen2.5-coder:7b`) and code questions, errors, failing tests and builds go to it - also "fix it" / "what's wrong here" while you're in your editor. Without it, the bigger model answers, then the main one.
- **Fallback**: if the fast / smart / coding model crashes, times out or was removed, Jarvis answers with the main model once and records it. If the main model fails, you're told.
- **Private stays local**: in Private mode (or for requests about passwords, bank, medical...), Jarvis only uses an Ollama on this PC or your own network, never one on the internet.
- **"Which model are you using?"** lists every model role, what's installed, and which model answered last (and any fallback). Details: [docs/model-router.md](docs/model-router.md).

### New in 3.18.0 - Personal knowledge graph

- Ask about your own world, with sources: **"which files are related to TMS"**, **"what decision did I make about the database"**, **"why did we choose PostgreSQL"**, **"which project uses Laravel"** / "contains AuthController", **"what was I working on last week"**, **"everything related to TMS"** - or Menu -> Knowledge.
- Links projects (their stack, key files, files changed in git over 14 days, your notes), memories and decisions (including what was replaced), goals, missions, and your requests / Jarvis's actions. Private memories stay out; nothing new is collected. Details: [docs/knowledge-graph.md](docs/knowledge-graph.md).
- Time phrases: "last week", "this month", "last month", "the last 3 days" now work in activity questions too.

### New in 3.17.0 - Memory 2.0: types, confidence, contradictions

- **Memory types**: facts, things that happened, how you like things done, and goals - recognised from how you say them (or say which). Finished missions are recorded as events, marked "recorded by Jarvis", never as something you said.
- **Contradictions**: "I migrated TMS to PostgreSQL" after "TMS uses MongoDB" replaces the old memory (kept as history, no longer used). "I use PostgreSQL for TMS" without saying it replaced anything asks you which is true: "keep the new one", "keep the old one" or "both are true".
- **Why do you remember that?** now also says the type, whether you said it or Jarvis recorded it, confidence, importance, what it replaced and open conflicts. The Memory window shows type and confidence. Details: [docs/memory.md](docs/memory.md).
- "Remember this, it's important" makes a memory go to the AI first.

### New in 3.16.0 - Context Engine: Jarvis knows what you're working on

- **"What am I working on?"** (or Menu -> What Jarvis sees): the app and window you're in, the project and file (from your editor's title, matched to projects you told Jarvis about), the git branch and uncommitted changes, your goal, the running mission, the approval waiting, recent actions, unread notifications and the PC's state - each with where it came from and whether it's known, probably right, or unknown (and why).
- **Continuity**: every request gets a short "Right now: ..." line, so "run the tests" means the project open in your editor, without naming it again. A folder you name always wins; only projects you told Jarvis about are used.
- **Private by design**: window titles only (never the screen), secrets redacted, off in Private/Presentation mode or with Settings -> Privacy -> "Understand what I'm working on from window titles"; snapshots are kept in memory for 15 minutes and never saved. Details: [docs/context-engine.md](docs/context-engine.md).

### New in 3.15.0 - JARVIS 4.0 track, step 1: secure phone identity

The plan from 3.14 to 4.0 is in [docs/V4_PLAN.md](docs/V4_PLAN.md) (what exists, what's missing, what Windows doesn't allow and the supported alternative, release by release).

- **Menu -> Mobile devices**: pair JARVIS Mobile with a 5-minute, one-use QR code; see each phone's key fingerprint, trust it (explicitly, in the window), restrict it to read-only, revoke it, rename it, choose its permissions (read, chat, approve, control, power), and see security events.
- **Cryptographic identity**: each phone is known by its own P-256 public key; the private key never leaves the phone. Every request is signed, time-stamped and one-use (replayed or old requests are refused), checked against the phone's trust and permissions, and then runs through the same safety checks and audit log as a spoken request.
- **Security alerts**: wrong pairing codes, forged signatures, replays and revoked phones trying again are recorded and announced.
- The Android side of this arrives in 3.16 (see [docs/ANDROID.md](docs/ANDROID.md)).

### New in 3.14.0 - Jarvis Wake: wake your PC from your phone

- **Jarvis Wake** (Android app of our own, in `android/JarvisWake`): one big "Wake up my PC" button. It sends the Wake-on-LAN magic packet (home broadcast, and your router's public address if set) and then waits until Jarvis on the PC answers - it only says "ON" when it really is.
- **Get it by QR code**: Menu -> Phone remote shows a "Wake app" QR code; the phone downloads the APK from Jarvis itself (home Wi-Fi / Tailscale only).
- **Set up in one tap**: on the phone remote page, "Set up the wake app" fills in this PC's name, wired MAC address, broadcast address, IP and port (read from Windows, paired phones only).
- **From another network**: router port forwarding or your router's own wake feature, set in Settings -> "Wake from outside". See [docs/PHONE_SETUP.md](docs/PHONE_SETUP.md) sections 6-7 for what the PC and router need.
- Build it once with `packaging\build.ps1 -Install -Android` (uses the official Microsoft OpenJDK and Google Android SDK tools, downloaded the first time).

### New in 3.13.1 - brightness check, Defender with your permission, phone setup

- **"Check brightness"** tells you per screen whether it takes brightness commands, and exactly what to do when it doesn't (monitor menu -> DDC/CI -> On; connect directly with DisplayPort/HDMI, not through a dock/adapter; graphics driver). Until then brightness works by dimming the picture in software, and Jarvis says so.
- **Defender updates**: Defender's updater needs administrator rights. Jarvis now asks **Windows** for them - the normal UAC prompt appears; press Yes. Nothing is elevated silently or without that prompt. The result is checked by Defender's signature version before and after.
- **Phone setup** (Menu -> Phone remote): a QR code to scan, the pairing code, and the steps. The page can be added to the phone's home screen (opens full screen like an app). Optional: reach it away from home through your own **Tailscale** network (off by default). Full guide: [docs/PHONE_SETUP.md](docs/PHONE_SETUP.md).

### New in 3.13.0 - Phase 10: skill versions, trusted plugins

- **Skill versions**: every time a workflow or automation is changed or deleted, the previous version is kept (the last 20). "show versions of study", "restore version 2 of study" (asks first; the restored steps are checked again like a new workflow).
- **Trusted plugins** (Menu -> Plugins): a plugin file only runs after you trust it in the window. Trusting records the file's sha256 and its exact tools and risk levels (`<name>.plugin.json`). A file changed afterwards is not run at all until you trust it again; a plugin can't add tools, lower a tool's risk or declare a blocked (CRITICAL) tool. Trusting is never possible by voice. **Existing plugins must be trusted once after this update.**

### New in 3.12.0 - Phase 9: research, video summaries, interpreter, study

- **Research** ("research the best laptop for programming"): Jarvis searches, *reads* the top pages and answers only from them, with numbered sources and their links. Pages are read as plain text from public addresses only (never your router or local services; every redirect is checked). Web text is untrusted: instructions in it are ignored, and actions after it that send or save things ask first.
- **YouTube summaries** ("summarize this video" for the one Jarvis is playing, or "summarize <link>"): from the video's captions. No captions = Jarvis says so; it never summarises from the title alone.
- **Interpreter mode** ("be my interpreter", "translate everything into Arabic"): everything you say is translated and spoken - Bangla <-> English by default, or into a named language. Nothing you say is treated as a command until "stop interpreting".
- **Study mode** ("quiz me on photosynthesis", "test me on my notes"): questions (from your indexed documents when you say "my notes"), your answers checked, explanations, the score at the end.

### New in 3.11.0 - Phase 8: Jarvis improves itself (safely)

Say **"improve yourself"** (or open Menu -> Evolution Center):

1. **Analyze** - Jarvis reads its own audit log (last 14 days) and lists what keeps failing, with the real numbers (e.g. "brightness_control failed 6 of 9 times with 'unsupported'"). Nothing is guessed.
2. **Propose** - an exact change (find/replace edits) is saved as a proposal with a diff and a risk estimate. Proposing changes nothing. The security core (permissions, executor, taint, emergency controls, pairing, settings, the evolution engine itself, the security tests, packaging) can't be changed this way at all; test files may not lose tests.
3. **Test** ("test proposal 3", always asks) - the source is copied to data/evolution/candidates/3 (without .venv, data, build), the change is applied to the COPY, and the project's own `pytest` and `ruff` run there (fixed commands, no shell). It passes only with 0 failures, 0 errors, at least as many passing tests as before, and a clean linter.
4. **Apply** ("apply proposal 3", always asks - also for your own voice command) - only a passed proposal whose files haven't changed since the test. Originals are backed up to data/evolution/backups/3, every written file is checked on disk. Then rebuild.
5. **Roll back** ("roll back the last update" / "roll back proposal 3") - restores the backup, but never over a file that was edited afterwards.

Code updates are manual only: there is deliberately no "apply automatically" policy. The emergency controls have a new switch, **self-improvement off** (voice can turn it on, only the window can turn it off). Set `JARVIS_SOURCE_DIR=D:\Jarvis` so the installed Jarvis knows where its source is.

### New in 3.10.1 - no more "I can't" for things Jarvis can do

- **Unknown app names**: "open Cloud" no longer gets a refusal. Jarvis lists the installed apps with that word in their name (iCloud Drive, Nextcloud...), suggests the one it sounds like, or offers a web search ("say yes").
- **Unknown sites in a named browser**: "open Netmirror in Google Chrome" searches for it in Chrome when Jarvis doesn't know the address.
- **Restart an app**: "restart Spotify" closes it normally and opens it again (never File Explorer, never the PC - that's still power_control).
- **"Minimize yourself", "show me my screen"**: Jarvis goes to the background. "Visualize my desktop", "what's on my screen" -> Jarvis looks at the screen (describe_screen).
- **Screen capture off**: Jarvis offers to turn it on ("say yes"); the privacy question still comes first.
- **"Yes, install it"** after an update list installs through windows_update (never run_command; update commands given to run_command are refused with the right tool named). Install also runs Microsoft Defender's own signature updater, so "Security Intelligence" updates land at once.
- **Brightness on any monitor**: DDC/CI now uses the low-level VCP brightness control when the high-level call fails (common), retries, and declares 64-bit handle types. A monitor with no DDC/CI at all is dimmed in software (a click-through veil, never fully black) - Jarvis says which one it used. Software dimming can only go darker than the monitor's own setting.
- Emoji the small model adds are removed from replies (unless you ask for emoji).

### New in 3.10.0 - Phase 7: devices, remote wake (honestly), remote status

- **"Can you turn on my laptop remotely?"** Jarvis checks this PC for real and answers from what it finds: lock /
  sleep / restart / shut down (supported), hibernate (only if Windows lists it), wake from sleep (only if a network
  adapter has "wake on magic packet" on), wake from full shutdown (at most "unknown": it also needs a wired adapter,
  the BIOS/UEFI option and Fast startup off - Windows can't confirm the BIOS part; Wi-Fi-only laptops: not
  supported). It then lists exactly what's needed - including that something on your home network that is ON must
  send the packet (another PC with Jarvis, your router's Wake-on-LAN page, or a Wake-on-LAN app on your phone with the
  MAC it shows), and that from outside you should use a VPN, never an open port.
- **Device registry**: this PC registers itself (detected in the background); "add a device desktop, MAC ..., address
  ...", "my devices", "is my desktop on?", "remove ...".
- **"Wake my desktop"** sends a Wake-on-LAN packet and then *checks*: "Wake request sent" -> "desktop is on (it answers
  on the network)" -> "Jarvis is ready there". If it doesn't answer within 2 minutes: "I couldn't confirm that the wake
  request worked" - never "it's on". A wake that really worked marks the device's Wake-on-LAN as verified.
- **"Hibernate"** (power control) when this PC supports it.
- **Phone status line**: the phone page shows "PC online · battery 72% (charging) · Jarvis 3.10.0 · remote wake: ...",
  or "PC not reachable" with the time when the PC doesn't answer.
- Database schema 13 (devices, device capabilities).

### New in 3.9.0 - Phase 6: notifications, modes, emergency controls

- **Notification center** (Menu -> Notifications, or "any notifications?" / "what did I miss?" / "clear my
  notifications"): reminders, battery and PC-watcher alerts, missions that paused or finished, security events (a
  new phone paired, a wrong pairing code, an action you refused after Jarvis read outside text), Jarvis updates. Each
  has a priority, a category and a time; filters, unread / archive, and a "Do it" button for actions Jarvis wrote
  itself ("clean temp files", "continue my mission") - never actions taken from outside text.
- **Event bus** underneath: every such event is logged (redacted, with the request's ID) and turned into
  notifications by fixed rules.
- **Modes**: "gaming mode", "meeting mode", "presentation mode", "private mode", "coding mode", "work mode",
  "research mode", "maintenance mode", "normal mode", "what mode are you in?". A mode decides what is spoken aloud
  (the rest waits in the notification center), suggestions on/off, short or normal answers; private mode saves
  nothing from conversations and records no habits; private and presentation modes refuse screen capture. Modes
  never grant anything, and they survive a restart.
- **Emergency controls**: "emergency stop" / "stop everything", **Ctrl+Shift+F12** anywhere, or Menu -> Emergency
  controls: cancels the request, pauses a mission, clears the song queue, cancels a shutdown countdown and switches
  automations off. Also: "lock yourself" (only read-only actions run), "disable network actions", "turn off the phone
  remote", "turn off my automations". **By voice these only switch things off; switching back on is only possible
  in the window** - so a voice from a speaker or video can't undo an emergency stop.
- Database schema 12 (events, notifications).

### New in 3.8.0 - Phase 5: the developer agent

- **Project profile**: "tell me about my project" - stack, dependencies, entry points, how to run / test / build it,
  the database it uses, git branch + uncommitted changes (remote shown without credentials), and your notes
  ("add a known problem: login breaks on Safari"). All read from the project's own files.
- **Search and read code**: "where is validate_user in my code", "search my code for login" (node_modules, .git,
  vendor, venv... skipped); the AI reads files with line numbers before changing them.
- **Code edits you approve**: the AI changes a file only by exact replacements (each piece of text must appear
  exactly once) or creates a new file - never a blind rewrite. Jarvis always shows the **diff** and asks; the
  original is copied to data\code_backups first, line endings are kept, and the result is checked byte for byte.
  **"Undo that"** restores it - but never over edits you made afterwards. Only inside projects you told Jarvis
  about; never protected folders.
- **"Run the tests" / "build my project"** uses the project's own command (npm test, python -m pytest, go test...)
  through the normal command policy, and summarises what failed. The AI follows inspect -> edit -> test -> report
  and may not claim a change worked when the tests fail.
- Code, test output and git output count as untrusted text (taint rules apply).
- Fixed: one direct-command pattern ("stop recording...") could match the start of a longer sentence; a new test
  checks every new pattern is anchored at both ends.

### New in 3.7.0 - Phase 4: computer use 2.0

- **Real switches**: "turn off Wi-Fi", "Bluetooth on", "turn on dark mode", "switch to light mode", "is Wi-Fi on?",
  "ওয়াইফাই বন্ধ করো". Wi-Fi and Bluetooth use Windows' radio API (new package winrt-Windows.Devices.Radios,
  installed by the build), dark mode your account's theme setting. Each change is read back; "undo that" switches it
  back. Turning Wi-Fi off asks first unless it's your own command. If Windows doesn't let apps control radios, Jarvis
  says so and opens the page.
- **Press controls by name** (Windows accessibility, no screen capture needed): "press Save", "click OK", "click the
  Settings tab", "tick Remember me". Buttons, links, tabs, menu items and check boxes are pressed with their own
  action; "save" never presses "Don't save". After pressing, Jarvis looks for a real reaction (state changed, window
  closed/opened/changed) and says "I can't see a change" when there's none. "Click the text ..." still uses OCR.
- **File tools** - always a NEW file next to the originals, never overwritten: "zip my projects folder", "unzip
  report.zip" (archives that would write outside their folder or expand past 2 GB are refused), merge PDFs, "make
  smaller copies of these photos" (…-small.jpg), convert images (PNG/JPG/WebP), Word to PDF (with Microsoft Word).
  The new files can be undone (moved to the Recycle Bin, which asks first).
- **Screen recording**: "record my screen" / "stop recording" - Windows' Xbox Game Bar records the app in front;
  on stop Jarvis checks that a new video really appeared in Videos\Captures.

### New in 3.6.0 - Phase 3: memory firewall, world model, context compiler

- **Memory firewall.** Every memory now records where it came from (you said it, you accepted a suggestion, you
  typed it, or you confirmed it after Jarvis had read outside text), which request it came from, whether it's
  private, when it expires and every earlier version.
  - "Why do you remember that?" / "where did you learn about my project folder?" answers from those records.
  - "Remember privately that ..." - private memories never go to the AI model; they're only read back when you ask.
  - "Remember for today that ..." / "remember until Friday that ..." - they're forgotten afterwards.
  - "Stop using that memory" (kept, but not used), "keep that private", "how did that memory change".
  - **Outside text can't plant memories:** after reading a page, email, message or document, anything the AI wants
    to *save* (a memory, nickname, contact, goal, automation, reminder) asks you first and says why.
  - The Memory window shows each memory's source and status (in use / off / private / expiry / version) and a
    "Why?" button.
- **World model.** "What do you know about my projects / me?" - projects (with their stack read from the folder's
  own files, and whether they're git repositories), goals, the unfinished mission, your most-used apps, contact
  names (never numbers), layouts, nicknames and remembered facts - each with its source, plus links (project uses
  PHP, goal is about project). Built from Jarvis's own records, never guessed.
- **Context compiler.** For each AI request Jarvis adds only the few facts that matter (running tests -> your
  project; "continue" -> the unfinished mission; a goal due within 3 days), within a small budget, marked as facts,
  not instructions. People only when the request names them. WORLD_CONTEXT=false turns it off.
- **"What can you do?"** answers from the tools that are really registered.
- Database schema 11 (memory provenance, revisions).

### New in 3.5.0 - Phase 2: goals, missions, dry run

- **Goals.** "My goal is to finish the TMS project this month" / "set a goal to learn Go by 15 October" /
  "what are my goals". Deadlines understand this week, this month, by Friday, 15 October, in 2 weeks, end of the
  year. Tasks ("add a task to my goal: login page", "I finished the login page") drive the progress; the morning
  briefing mentions goals due within a week.
- **Missions** - multi-step tasks that survive a restart ("prepare everything for tomorrow's presentation"). The AI
  proposes a plan of ordinary tool calls; Jarvis validates it (real tools, valid arguments, no automation/power/
  memory-wide tools as steps, at most 12 steps), **simulates** it (risk of each step, what still asks, what can be
  undone) and asks you to approve the plan. Approving covers exactly those steps: moderate ones then don't ask again,
  dangerous ones still ask one by one, critical ones stay blocked. Each step is saved as it finishes; a failed
  read-only step is retried once, anything else pauses the mission: "retry the mission", "skip that step",
  "cancel the mission", "how is my mission going". A mission cut off by a restart continues on "continue my mission".
- **Dry run**: ask to "just show the plan" and the mission is saved as planned without doing anything.
- **Mission Control** has a second tab: goals with their tasks, missions with each step's state.
- Database schema 10 (goals, goal_tasks, missions, mission_steps).

### New in 3.4.1 - Phase 1 done, Phase 2 started: undo, verified results, Mission Control

- **"Undo that" / "undo your last action" / "take that back"** (Bangla: "আগের কাজটা ফিরিয়ে দাও"). Jarvis keeps the
  exact reverse of actions it can reverse - a move, a rename, a new file or folder, a volume or brightness change -
  for 24 hours, and runs it through the normal checks (moving a new folder to the Recycle Bin still asks).
  "What can you undo?" lists them. Bare "undo" is still Ctrl+Z in your app.
- **Verified results.** File actions (create, rename, move, delete) are checked on disk afterwards; if the check
  fails, Jarvis says "...but when I checked, it isn't there" instead of claiming success, and offers no undo.
- **Mission Control** (Menu, or "open mission control"): every request with where it came from, how it ended,
  how long it took, and its full trace - permission level, your answers, checks, results - plus the undo list.
- **Under the hood:** the 2,000-line window controller is split into five parts (features, health, remote,
  background, voice); every setting belongs to a named group (app/config/groups.py) with a test that keeps it so;
  database schema 9 (undo records).

### New in 3.4.0 - Phase 1 foundation: security first (see docs/ARCHITECTURE_AUDIT.md)

- **Prompt-injection defence (taint tracking).** After Jarvis reads text written by others (web pages, email,
  WhatsApp, news, MCP servers) or private text (documents, the screen, clipboard, files), anything the AI then
  wants to *send out* (open a website, search, message, email) runs on its own only when every word is yours or
  came from what it read, and any site is one you named or a well-known one. Otherwise it asks, and says why.
  Your own direct commands are never affected. Rules: app/security/taint.py (deterministic, not the AI).
- **Phone remote pairing v2.** The address no longer contains the key. Menu -> Phone remote shows a one-time
  6-digit code (5 minutes; 5 wrong codes lock pairing for 10 minutes); each phone gets its own key, sent in a
  header; only a hash is stored (DPAPI-encrypted). **Pair your phone again once** after updating.
- **Answers are bound to the question.** Every confirmation has its own ID; the phone's Yes/No names it, and a
  stale answer ("that question is no longer waiting") is refused.
- **First run of a project's own code asks once.** `npm test`, `npm run dev`, `pytest`... in a project you haven't
  used with Jarvis before: "First time running code from X... trust this project?" Your DEV_PROJECT_DIR and the
  project folder you told Jarvis about are trusted from the start.
- **State machine.** The HUD state (Ready, Listening, Thinking, Working, Needs your OK, Speaking, Problem) is now an
  explicit state machine with transition rules and a watchdog: a busy state that isn't true any more ends by
  itself (no more stuck "Thinking"/"Problem").
- **Runs.** Every request (voice, text, phone, automation) gets an ID; its audit entries carry it
  (`agent_runs` table, schema 8). Requests cut off by a crash are marked "interrupted" at the next start.

### New in 3.3.0 - YouTube control, window layouts, developer commands, WhatsApp, your own commands

- **YouTube in the music tab**: "skip the ad", "like this song", "forward 30 seconds", "go back 10 seconds",
  "start the song again", "captions on", "make the video full screen", "mute the video", "what song is this".
  Keys use YouTube's own shortcuts and are sent only after checking the browser has the focus; Skip Ad and Like
  are pressed through Windows UI Automation by their accessible names (honest failure if they aren't there).
- **A song queue**: "after this play Arijit Singh", "play Kesariya next", "what's in the queue", "clear the
  queue", "play this again" (repeat once), "next song" (takes the queue first). A watcher reads the YouTube tab
  title every 2 s; when the song ends and YouTube moves on, the next queued song loads in the same tab - only
  while you aren't typing, and your window gets the focus back. With autoplay off YouTube stays on the finished
  song, so say "next song".
- **Window layouts**: "put Chrome on the left", "move this window to the right", "snap VS Code top right",
  "Chrome on the left and VS Code on the right", "move Chrome to my second screen", "maximize everything",
  "save this layout as work", "work layout" (moves windows back and starts apps that are closed), "list my
  layouts", "delete my work layout". Saved in layouts.json in the data folder.
- **Developer**: "run my project" reads how to start it from the project's own files (package.json dev/start/
  serve, manage.py, artisan, run.py/main.py/app.py, Cargo.toml, go.mod, pubspec.yaml) and goes through the same
  command policy as every other command (unapproved commands ask first). PHP projects in XAMPP's htdocs open
  on http://localhost/<folder>/. "commit my changes with message fix login" (asks first; "and push" asks as a
  dangerous action). "What's this error?" reads the screen and explains it (needs SCREEN_CAPTURE_ENABLED=true).
- **WhatsApp**: "any new WhatsApp messages?" reads the unread chats from the WhatsApp app's window (read-only,
  nothing is opened or marked read). "Summarize my unread emails" reads classic Outlook.
- **Your own voice commands without code**: Menu -> Automations -> "New voice command...": a phrase ("gaming
  time") and steps (close app, open app, website, music, volume, brightness, layout, focus, run project, say).
  Saying the phrase runs it; steps keep their usual permissions.
- **Self-check**: if the browser doesn't start at all, the page opens in Windows' default browser instead.

### New in 3.2.2 - one tab for songs, exact volume and brightness

- **Songs play in the same tab.** "Play X" again (or "search X on YouTube") reuses the YouTube tab that is
  already open: Jarvis finds it (the window title, else the browser's tab strip through Windows UI Automation,
  else Chrome/Edge's own tab search), brings it in front and loads the new song there. No pile of YouTube tabs.
  Keys are only sent after checking that the browser window really has the focus; if it can't be brought in
  front, a new tab opens instead. "Open YouTube" while a song plays just shows that tab (the song keeps playing).
- **Other sites open in a new tab** (Facebook, Gmail, ...) of the same browser and profile.
- **One profile.** Jarvis remembers the Chrome/Edge profile it used (`browser_profile.json` in the data folder)
  and keeps using it, so pages no longer jump between profiles. Name another profile once ("open Chrome Night
  profile") and everything follows it. `REUSE_TABS=false` brings back a new tab every time.
- **"Volume set at 50", "brightness set at 70"** work in any word order, with words or digits, English or Bangla:
  "set the volume to 35%", "make the volume forty", "increase volume to 70", "volume up by 20", "max volume",
  "brightness down by 15", "full brightness", "what's the brightness", "ভলিউম ৫০ করো", "ব্রাইটনেস ৮০ করো".
- **New `brightness_control` tool**: laptop screens through Windows' own brightness (WMI), external monitors
  through DDC/CI. It reads the level back to confirm, and says so when a screen doesn't allow it.

### New in 3.2.1 - Jarvis gets out of the way

- **You see what Jarvis opened.** After opening or searching something (an app, a website, a YouTube or
  Google search, music, a folder, a Settings page...), the command center fades out into the mini orb and
  that app comes to the front. The orb keeps showing Jarvis's state and hands-free keeps listening; click
  the orb or say "show yourself" and the command center fades back in, panels drawing themselves again.
  `STEP_ASIDE=true`; with "stay on top" it stays.
- **Keys, text, scrolls and clicks go to your app.** Before typing, pressing a shortcut, scrolling or
  clicking, Jarvis steps aside and gives your app the keyboard (a search was once typed into Jarvis's own
  window, and "scroll down" scrolled Jarvis).
- **"Search Imran Hashmi on YouTube"** ("search YouTube for ...", "open YouTube and search ...", "search
  ... on Google", "ইউটিউবে ... সার্চ করো") is a direct command now: it opens the search in your browser
  instead of leaving it to the AI.

### New in 3.2 - fast answers: the AI model fits on the GPU

Your answers took 5-14 s for ~2,000 tokens in and ~40 out - a 4B model entirely on a GPU does that in
1-2 s. On a 6 GB GPU shared with speech recognition, the model didn't fit and ran partly on the CPU.

- **Ollama starts with memory-saving settings** (when Jarvis starts it, as at sign-in): flash attention
  and an 8-bit conversation memory (KV cache) - about half the GPU memory for the context, with no
  noticeable change in answers. `OLLAMA_FAST_MEMORY=true`; a value you set yourself wins.
- **Measured, then fitted.** After each warm-up Jarvis asks Ollama how much of the model is in GPU
  memory. If it isn't all there, it loads it again with a smaller context (6,144 -> 4,096 tokens) until
  it fits, and tells you once if it still can't (with what helps).
- **Reported:** the speed report shows "AI model on the GPU: 100%", the health report tracks it, and
  every answer's time is logged split into loading / reading the prompt / writing, so the next slow
  spot is visible instead of guessed.

### New in 3.1.4 - "close everything"

- **"Close everything"** ("close all apps", "close all windows", "সব বন্ধ করো", "close everything except
  Chrome") closes every open app at once, normally - like clicking X on each, so apps with unsaved
  work still ask you first. Jarvis, the desktop and taskbar, and Windows' own parts stay; background
  services and processes without a window are left alone (ending those can break Windows). It's a
  direct command now: it used to go to the AI, which didn't answer in time.
- **The first question no longer waits minutes.** The model was warmed up at a different context size
  than the chat asks for (and the GPU memory saver changed it afterwards), so Ollama loaded the model
  again on the first question - with speech recognition holding GPU memory, partly on the CPU (over
  two minutes in your recording). The warm-up now uses the chat's size, and runs again when it changes.

### New in 3.1.3 - "PROBLEM" clears itself

- When Jarvis starts before Ollama (at sign-in), it shows PROBLEM while the AI can't be reached. The
  window painted that state itself, without the controller knowing, so when the AI connected nothing
  cleared it: "Connected to the AI model" appeared, but the reactor stayed red. The controller now owns
  the state: PROBLEM while the AI is away (never in the middle of a reply), back to READY as soon as
  it answers.

### New in 3.1.2 - continuous animation, no pauses

- **The pauses are gone.** Every 6 seconds the dashboard listed all running processes (Chrome alone has
  dozens) on the window's own thread, which froze every animation for ~0.7 s (measured in your screen
  recording: 0.6-0.9 s stops at 4.2, 7.0, 10.2, 16.3, 22.4 and 28.3 s). All system measurements now
  run in a background thread; the window only shows the numbers. The 30-minute refresh of the speech
  hints moved off the window thread too.
- **Even frame timing.** The animations use precise timers (Windows' default timers snap to ~15.6 ms,
  which makes 30 fps judder), and dashboard rows that didn't change are no longer restyled every tick.
- **Robust:** one animation that fails can no longer stop all the others.
- Measured in the real window: 30 fps idle, 50 fps thinking / speaking, worst gap between frames
  36 ms / 25 ms, no pause longer than that in 20 seconds (with the stats updating).

### New in 3.1.1 - fixes from your screen recording

- **Jarvis comes to the front.** Started at sign-in, Windows kept Chrome in front and the command
  center stayed behind it. Now the intro (which is in front) hands the focus over, and Jarvis puts
  itself on top (briefly topmost, then the focus). "stay on top" keeps it above every window
  (`ALWAYS_ON_TOP`), "stop staying on top" undoes it.
- **No more terminal windows popping up.** They were Ollama's model runners: the server was started
  without a console, so every runner opened its own terminal window. It now starts with a hidden
  console that the runners share.
- **A smooth intro.** It ran in the same process as the start-up work (loading speech recognition on
  the GPU, starting Ollama), which starved it of frames. It now runs in its own process, starts at
  once (before the window is even built), shows the live states, and waits at its last moment with
  the rings still turning until Jarvis has loaded (at most 25 seconds).
- **Smoother HUD.** The background only repaints the gaps between panels (repainting under a panel
  made Qt repaint the whole panel), running lights repaint only the title line, and the reactor draws
  its unchanging layers once: 8.6 -> 5-6 ms per frame, so it now runs at 30 fps idle / 50 fps active
  (was 15 / 30). It looks exactly the same.

### New in 3.1 - hears short commands, a real wake engine, a cinematic start

- **Short commands no longer turn into "Thank you."** A short clip that comes out as a filler ("Thank
  you", "All right", "Thanks") is recognised once more, louder and without the voice filter that can
  cut short words; if it is still a filler and the recognizer isn't sure, it counts as not heard
  instead of being acted on. (Your health report: "Volume up" -> "Thank you.", "Minimize Chrome" ->
  "Thanks.")
- **Benchmark recorder fixed** - 12 of 22 recordings were empty (every Bangla one): the voice
  detector missed quiet speech. It now waits longer before stopping, falls back to a fixed 4-second
  recording that is trimmed to your speech, tells you when the microphone is too quiet, and shows
  each Bangla prompt romanized too (consoles often show Bangla as boxes). Run
  `jarvis-cli --record-benchmark` again - only the bad recordings are redone.
- **Wake engine for "Jarvis"** - the neural "hey jarvis" model now listens for a bare "Jarvis" too.
  Measured on synthesized speech it scores most spoken "Jarvis" highly (other words stayed below
  0.33); any sentence it flags (score 0.08+) and every short one is checked by speech recognition for
  the name, so wake-ups stay as reliable while ordinary talk is no longer transcribed at all.
- **Bangla answers** - facts it isn't completely sure of (a country's symbols, history, geography,
  science) are looked up with web search first, searched in English, answered in Bangla. The health
  report and "compare models" load each model before asking (the last report timed out on question 1).
- **Cinematic start, full screen** - when Jarvis starts (also with Windows: START_WITH_WINDOWS=true)
  a ~4 second full-screen intro plays: the hex grid lights up, HUD rings assemble and spin (ticks,
  sweeping arcs, gold dashed ring, radar sweep, orbiting points), the name decodes letter by letter,
  and a boot log shows the real state of each part live (speech output, recognition, AI model,
  hands-free, wake word, memory) next to this PC's real CPU and memory. A flash, the rings fly
  outward, and the command center fades in full screen while its panels draw themselves in. Any key
  or click skips. F11, "make yourself full screen", "exit your full screen". `INTRO_ANIMATION`,
  `START_FULLSCREEN`.
- **Livelier HUD** (centre reactor unchanged): the header logo's three rings turn against each other,
  a light sweeps across the title, the power core has a turning dashed ring, an orbiting spark and a
  bright tip, panels glow and stretch their corners when you point at them, a light runs along every
  panel title while Jarvis thinks or speaks, and motes drift across the background in the state's
  colour.

### New in 3.0 - focus, a better briefing, smarter search, a livelier HUD

- **Focus mode** - "focus for 45 minutes" (also "start focus mode", "do not disturb for 2 hours",
  "৪৫ মিনিট মনোযোগ মোড চালু করো"). While it runs Jarvis keeps quiet: no habit suggestions, no weekly
  check, and PC-watcher notes and non-urgent alerts are held back (a critically low battery still
  speaks). "...and close distractions" also closes the apps in `FOCUS_APPS` (you're asked first -
  unsaved work in them could be lost). At the end you hear how long you focused and what was held
  back. "stop focus", "how much time is left on my focus".
- **Richer morning briefing** ("good morning", "brief me"): today's meetings and the unread email count
  from classic Outlook - only if Outlook is already open, it is never started for this - and, in the
  morning, what you were working on last time ("say 'continue where I left off'").
- **Find files by what's inside** - "find the PDF about my thesis I downloaded last week" searches
  inside PDF, Word and text files too (newest 150, first pages, at most 12 seconds), name matches first.
- **Better hearing of names** - the recognizer is now primed with your nicknames, the apps you open
  most, your workflows and your contacts, refreshed every 30 minutes (no restart needed). Bangla
  corrections in Bangla word order work: "না, আমি ফটোশপ বলেছি".
- **Answers that fit** - "tell me more" / "explain in detail" / "বিস্তারিত বলো" gives a full answer
  even by voice; "in one line" / "সংক্ষেপে" a one-sentence one. Facts that change over time (news,
  prices, scores, who holds a job) are looked up on the web when online, and arithmetic goes to the
  calculator, instead of being answered from memory.
- **HUD motion** (the centre reactor is unchanged): panels draw themselves in one after another with
  their titles typed out, meters and the power core glide to new values and fill up at start, a light
  runs along panel titles now and then, and a slow scan beam and pulsing hexagons move over the
  background in the colour of Jarvis's state (listening green, thinking gold). One shared timer, idle
  when nothing moves; `ANIMATIONS=false` or "turn off animations" keeps everything still.

### New in 2.9 - it remembers what you were doing

- **"What was I doing before lunch?"** (also "what did I do yesterday", "what did you do for me this
  morning", "last 2 hours", "গতকাল আমি কী করেছি?"). The answer comes from Jarvis's own log of
  actions it actually carried out - apps, websites, music, searches. Apps you open by hand aren't in
  that log, and Jarvis says so instead of guessing.
- **"Continue where I left off"** ("pick up where we left off", "restore my last session", "আগের কাজ
  আবার খোলো") reopens the apps (with their Chrome profile), websites, folders and projects from your
  last session - apps you closed again are skipped. A session ends after a 2-hour pause. Every item
  goes through the normal permission checks.
- **"Save this as my work setup"** saves what you opened in this session as a workflow (you're asked
  first); later "start my work setup" opens it all again.
- **Routines** - the habit suggestion now offers your usual set together: "You usually open VS Code,
  Chrome and Spotify around now. Shall I open them?" One yes opens all of them.
- **Screen questions** - "what does this error mean", "summarise this page", "explain this" now get the
  screen tools (screen capture must be on: "turn on screen capture").

### New in 2.8 - fixes found in your own usage logs

- **No more "context size exceeded".** A message that names no tool group ("thanks", "how are you")
  used to send all 98 tools (~14,000 tokens) - more than the 6,144 tokens the GPU memory saver
  allows, so the answer failed. Now only the everyday groups go (apps, info, web), tools are held to
  40% of the model's window, old history is trimmed for that one request when needed, and if the
  model still says it's too long, Jarvis retries once with just the basics.
- **Chrome profiles with the same name.** "Open Chrome labib" with several "Labib" profiles opens the
  one you used last (and says which), instead of asking every time. Say "open chrome profile 3"
  for a specific one.
- **Did you mean...?** An app name that was misheard ("WordShop") gets "Did you mean Photoshop? Say
  yes to open it." Saying yes opens it and remembers the nickname, so next time it just works. The
  same for closing / switching to an app that isn't open. Only app tools can be suggested.
- **Small argument mistakes are repaired**, e.g. `sort_by: "extension"` -> the default, a near-miss
  choice -> the right one, an extra field -> dropped - **only when the repaired call is SAFE** for its
  exact arguments (a guess can never turn into restart, delete or anything that needs asking).
- **Bangla heard as English.** When Whisper picks English but is unsure and gave Bangla a real chance,
  the sentence is also recognised as Bangla and the surer result wins (GPU only; forced languages are
  left alone).
- **Use the best model by voice.** At start Jarvis tells you when answers don't come from "jarvis".
  Say "compare models", then "use the best model" (it compares first if needed). No model is deleted
  by voice.
- **Weekly health check.** Once a week, after 15 minutes without use, the health report runs on its
  own and speaks only if something got worse (`WEEKLY_HEALTH_CHECK=true`; "turn off weekly health check").
- **Updates are not crashes.** The installer closing the old version no longer leaves a crash report
  (a real native crash still does).

### New in 2.7 - measure, then improve

- **Health report** - say *"health report"* (or Menu -> Health report, or `jarvis-cli --health-report`).
  It runs the self-test, the voice benchmark (if you recorded it), 8 Bangla questions, your typical
  thinking/hearing times and last week's mistakes, saves `data\reports\health-<time>.md`, and tells you
  what got **better or worse** since the last report. Every number comes from a real run.
- **Which model is best?** - say *"compare models"* or run `jarvis-cli --compare-models`
  (or `--compare-models qwen3:4b-instruct,qwen3:8b`). Every installed chat model (up to 4) answers the
  same Bangla questions; you get score, fully-right answers and time per answer. Close scores (within 3
  points) go to the faster model. `jarvis-cli --compare-models --switch` also rebuilds the `jarvis`
  model from the winner. By voice it only reports - switching stays your explicit step.
- **Bangla speech model (optional)** - set `STT_BANGLA_MODEL` (Settings -> "Bangla speech model") to a
  Bangla-tuned Whisper in **CTranslate2 format** (a folder path, or a Hugging Face repo that already
  contains a CTranslate2 model). When a sentence is detected as Bangla it is recognised again with that
  model; if the model can't load, Jarvis turns it off for the session and keeps using the normal one.
  Jarvis does not ship one. To convert a Transformers Whisper checkpoint yourself:

      pip install ctranslate2 transformers[torch]
      ct2-transformers-converter --model <bangla-whisper-repo> --output_dir D:\models\whisper-bn-ct2 --quantization float16 --copy_files tokenizer.json preprocessor_config.json

  then `STT_BANGLA_MODEL=D:\models\whisper-bn-ct2`. Check with the voice benchmark before and after.
- **Plan and check for multi-step tasks** - for "research X and make a document"-style requests the
  model is told to check every tool result and retry a failed step once another way, and never to claim
  a failed step worked. Afterwards a checklist built from the *real* tool results is shown (not read
  aloud): `Steps (2 of 3 worked): 1. ✓ web_search ... 2. ✗ write_document - permission: ...`.

### New in 2.6 - everything by voice

- **No clicking needed:** Jarvis's own screens (*"open your settings"*, *"open memory"*, *"privacy
  dashboard"*, *"show logs"*, *"close the dialog"*, *"hide yourself"*, *"show yourself"*), its features
  (*"enroll my voice"*, *"learn my wake word"*, *"train my voice"*, *"make the jarvis model"*, *"run self
  test"*, *"speed report"*, *"new conversation"*) and its settings (*"turn on screen capture"*, *"set voice
  speed to 1.2"*, *"set language to Bangla"*, *"turn off speak replies"*). Questions are answered by voice.
- **The window you're in:** *"close this"*, *"minimize this"*, *"what window is this"*; app shortcuts by
  name: *"new tab"*, *"next tab"*, *"full screen"* (YouTube), *"save as pdf"* (Word), *"copy"*, *"paste"*,
  *"undo"*, *"zoom in"*, *"go back"*.
- **Mouse and typing:** *"click"*, *"double click"*, *"right click"*, *"move mouse left 200"*, *"scroll
  down"*, *"click the gear icon"*; *"start dictation"* types everything you say ("comma", "full stop",
  "new line") until *"stop dictation"*.
- **Nicknames:** *"call Visual Studio Code 'code'"*, *"my university means diu.edu.bd"* - then just say it.
- **Learns from its mistakes:** what it couldn't find, what you cancelled or corrected is noted; *"review
  your mistakes"* (it reminds you weekly) suggests fixes - *"accept fix 1"* / *"accept all fixes"*.
- **Answers you like:** *"good answer"* keeps it as an example the model follows for similar questions;
  *"bad answer"* is noted for the review.
- **Routines on events:** automations can run when the PC joins a Wi-Fi network, an app opens, or a USB
  drive is plugged in; a workflow's name alone runs it (*"good night"*).
- **PC watcher:** warns when the disk is almost full (*"clean temp files"*), an app hogs the CPU, the GPU is
  hot, or a big download finished (`PC_WATCHER`).
- **Find files by meaning:** *"find the PDF about machine learning I downloaded last week"*, *"open my latest
  thesis draft"* (Desktop, Documents, Downloads).
- **WhatsApp:** *"send WhatsApp to Rafi saying I'm on my way"* - it asks "send it?" and you answer by voice.
- **Kokoro offline voice:** a natural English voice without internet (`TTS_ENGINE=kokoro`, 120 MB once,
  checksum-verified); with the neural voice it takes over when the internet is down.
- **Self-healing:** the automation loop and Ollama are restarted if they stop.

### New in 2.5

- **Just say the name:** *"YouTube"*, *"Google Chrome"* (opens with your CHROME_PROFILE, "last" by
  default), *"Spotify"*, *"bluetooth"*, *"dark mode"*, *"device manager"*, *"screenshot"*, *"mute"*,
  *"news"* - a keyword on its own opens or does it, instantly, without the AI. Sentences still go to the AI.
- **Windows Settings by keyword:** 45 Settings pages (display, sound, wifi, bluetooth, power, storage,
  apps, default apps, startup apps, wallpaper, taskbar, date & time, language, printers, privacy,
  Windows Security ...) and system tools (device manager, services, event viewer, control panel, disk
  cleanup ...): *"bluetooth settings"*, *"open display"*, *"device manager"*.
- **Windows Update:** *"scan windows update"*, *"update windows"*, *"check for updates"*: Windows Update
  opens, checks, downloads and installs right away - no question when you say it yourself (the AI
  deciding on its own still asks). Windows says when a restart is needed; Jarvis never restarts by itself.
  *"which updates are waiting?"* lists them.
- **One model called "jarvis":** Menu -> *Make the Jarvis model* (or `jarvis-cli --make-jarvis-model`)
  makes the Ollama model "jarvis" from the best installed one and switches to it; it shares that
  model's files, so it takes seconds and no extra space. Tick the box to delete the other chat models
  (embedding, vision and fast/smart models are kept). To upgrade later: `ollama pull <better model>`,
  then `jarvis-cli --make-jarvis-model --model <better model>` - "jarvis" is replaced.

### New in 2.4

- **See what changed:** every `--benchmark` run is compared with the previous one (accuracy and
  speed, which commands now work and which stopped working). `jarvis-cli --eval-bangla` scores the
  AI's Bangla answers on 15 fixed questions with simple rules (Bangla script, right fact, short
  enough); add `--model qwen3:8b` to compare another model. Results are kept in `data\eval`.
- **Faster answers:** on the GPU, the command is recognized at your first short pause while
  Jarvis waits to be sure you've finished; if you keep talking, that result is dropped
  (`EARLY_TRANSCRIPTION`). This saves about one recognition time per command.
- **Checks its own work:** after minimizing, maximizing or switching windows, setting the volume,
  opening an app or clicking on the screen, Jarvis checks it really happened, and says so honestly
  if it didn't ("I clicked, but nothing on the screen changed").
- **Your wake word:** Menu → *Teach me my wake word* records "Hey Jarvis" five times. Wake-ups
  that don't sound like your recordings (a TV line, a similar word) are ignored
  (`PERSONAL_WAKE_CHECK`). It is a check on top of the wake-word model; only features are kept.
- **MCP servers:** connect Jarvis to MCP servers (GitHub, Notion, file systems and hundreds more) in
  `config\mcp.json` — see `config\mcp.example.json`. Every MCP tool asks before it runs unless
  you give it a level; tools a server marks destructive always ask; their output is untrusted data.
- **Memory suggestions:** when you mention something lasting ("my thesis is due 15 October"),
  Jarvis offers it as a memory (Menu → Memory → Suggestions). Nothing is saved until you accept;
  rejected ones are not offered again; secrets never get in.
- **Phone companion:** the phone page is now https (`REMOTE_HTTPS`), so you can hold 🎤 and talk from
  the phone (the PC recognizes it; audio is not kept) and turn on 🔔 alerts for reminders and
  answers. The phone warns once about the self-signed certificate: Advanced → Proceed; Jarvis
  shows its fingerprint so you can check it.
- **Tone awareness:** if you sound rushed or tired compared with *your* usual speaking speed and
  loudness, the next answer is shorter and gentler (`TONE_AWARE`). Only two running averages are kept.
- **Encrypted secrets:** secret settings (e.g. `HOME_ASSISTANT_TOKEN`, written as `dpapi:...`),
  contacts, the voice print, wake-word recordings and the phone-remote key are encrypted for your
  Windows account (DPAPI). Tokens typed into `.env` by hand are encrypted at the next start.
- **Safer updates:** downloads are checked by SHA-256 and Windows code signature; a broken
  signature is always refused, unsigned ones when the release says it is signed or
  `UPDATE_REQUIRE_SIGNATURE` is on. CI now also builds the app on Windows and smoke-tests the exe.

### New in 2.3

- **Voice training:** record your commands once (`jarvis-cli --record-benchmark`), then Menu →
  *Train my voice* (or `jarvis-cli --train-voice`). Jarvis compares what it heard with what you
  said and learns your pronunciation ("close phone lane" → "close Phone Link", "chrome club" →
  "ক্রোম খোলো"). A fix is kept only if it makes that command work and breaks none that worked.
  The benchmark now uses what was learned, and more Bangla spellings of the name wake it.
- **Meeting / lecture notes:** *"start lecture notes"* / *"লেকচার নোট নাও"* (or Menu). Every
  sentence is written to `Documents\Jarvis\Notes` as it is spoken; *"Jarvis, stop the notes"*
  ends it, and the AI writes a Word document with a summary, key points, decisions and action
  items plus the full transcript. Audio is never saved. A TAKING NOTES badge shows while it listens.
- **Clipboard helper:** *"translate what I copied into Bangla"*, *"summarise my clipboard"*,
  *"copy that"*. Copied passwords and keys are refused, never read.
- **To-do list:** *"add buy milk to my list for tomorrow"*, *"what's on my list"*, *"mark milk
  done"*. The morning briefing mentions what is due.
- **Bigger model for hard questions:** set `AI_SMART_MODEL` (e.g. `qwen3:8b`): explain / write /
  compare / plan requests use it, everyday requests stay on the fast main model.
- **Faster first answer:** the model starts loading while you are still speaking.
- **Speed report** (Menu): typical and slow times for hearing, thinking and speaking, which model
  answered, and tips. Only numbers are kept (`data\speed.json`).
- **Click by description:** *"click the gear icon"*, *"click the blue Send button"*: text is found
  with OCR; icons and pictures with the vision model (`qwen2.5vl:3b`). It always asks first and
  needs screen capture to be allowed.
- **Sort Downloads:** *"sort my downloads"* moves loose files into folders by type;
  `AUTO_SORT_DOWNLOADS=true` does it every few minutes (files still downloading are left alone).
  *"undo sort downloads"* puts them back.

### New in 2.2

- **Learns your corrections:** when it mishears, say *"no, I said Phone Link"* (or *"না, আমি বলেছি
  ফোন লিংক"*). It redoes the request with the right words, remembers the fix for next time and
  adds the word to the recognizer's hints. Stored in `data\corrections.json`.
- **Your own words:** `CUSTOM_VOCABULARY` (Settings → Voice input → *Words I use often*): names,
  places and apps, comma-separated, so they are recognized first.
- **Second listening pass:** on an NVIDIA GPU, an unsure sentence is decoded again with a wider
  search before it asks you to repeat — helps most with Bangla and names (`STT_SECOND_PASS`).
- **Crash reports:** if Jarvis closes unexpectedly, the next start saves a redacted report in
  `data\crash-reports` and tells you where it is. Nothing is sent anywhere.
- **Faster start:** the window appears first; the voice and always-listening start just after.
- **Multi-step tasks:** *"research the best laptops under 1000 dollars and make a document"* —
  it writes a short plan, carries it out with the tools, and saves a Word document in
  `Documents\Jarvis` (never overwrites).
- **Sees your screen with a local model:** the setup wizard offers `qwen2.5vl:3b` for
  *"what's on my screen"* (screen capture must be allowed in Settings → Privacy).
- **Smart home (Home Assistant):** set `HOME_ASSISTANT_URL` and `HOME_ASSISTANT_TOKEN` (Settings →
  Smart home), then *"turn off the bedroom fan"*, *"is the front door locked?"*. Lights, fans and
  switches run directly; locks, alarms and garage doors always ask first. The token is never logged.
- **New settings window:** sections in a sidebar, HUD styling, and a search box that finds any
  setting by name, description or `.env` key.
- **Privacy dashboard** (Menu → *Privacy dashboard*): everything Jarvis keeps on this PC —
  memories, conversation history, document index, habits, activity log, contacts, corrections,
  voice print, benchmark recordings, crash reports, old logs — with *View* and *Delete* for each,
  plus *Renew phone-remote key*.

### New in 2.1

- **Self-test:** `jarvis-cli --selftest` (or Menu → *Run self-test*) tries every feature for real and
  prints PASS / FAIL / WARN / SKIP with timings. It covers the AI model, embeddings, English and Bangla
  speech, a full voice round trip (speech → recognition → command), Bangla commands, the microphone,
  the wake word, the voice print, the GPU, the internet, Outlook and the camera. Nothing is opened or
  sent, and nothing is played aloud.
- **Your own voice benchmark:** `jarvis-cli --record-benchmark` records 22 real commands in Bangla,
  English and mixed. `jarvis-cli --benchmark` replays them and reports how many commands were understood,
  how close the text was, and the recognition time. Each run is saved in `data\benchmark`, so you can
  compare runs after changes.
- **Watchdog:** if listening, speech output or presence detection stops, it is restarted and you're told.
- **Memory:** after `IDLE_UNLOAD_MINUTES` without requests, the AI model's memory is freed; it reloads
  when you say "Jarvis". `GPU_MEMORY_SAVER` gives the AI a smaller context on GPUs with 8 GB or less, so the
  AI and speech recognition both stay on the GPU.
- **Screen agent** (needs screen capture allowed): "click Submit", "type hello", "press ctrl+s",
  "scroll down". It reads the screen and moves the mouse and keyboard. Every action asks first, it
  never types passwords or keys, and blocked windows (password managers, banking) are never touched.
- **Phone (via Phone Link):** "save Ammu's number as 017...", "call Ammu", "text Rafi I'm on my way". The
  message opens ready to send, and you press Send. Reading your phone's messages isn't possible, because
  Phone Link doesn't allow it, and Jarvis says so.
- **Suggestions:** "You usually open VS Code around now, shall I open it?" This is learned only from
  apps you open with Jarvis, stored on this PC, and each app is suggested at most once a day
  (`PROACTIVE_SUGGESTIONS`).
- **Offline Bangla voice:** a local Bangla voice (108 MB, SHA-256 pinned, downloaded once in the
  background) speaks Bangla replies without internet. The neural online voice is still used when online.
- **Phone remote** (`REMOTE_ENABLED`): Menu → *Phone remote* shows a private address such as
  `http://192.168.1.5:8765/?t=...`. Open it on your phone on the same Wi-Fi to chat, use quick buttons and
  answer confirmations. It only answers devices on your home network that have the key, and everything
  goes through the usual checks.

### New in 2.0

**Voice safety and conversation**

- **Answer by voice:** when Jarvis asks "Empty the Recycle Bin?", say "yes" / "হ্যাঁ" or "no" / "না"
  (`VOICE_CONFIRM`). The dialog works as well.
- **Only your voice (optional):** use Menu → *Enroll my voice* and read four sentences, then turn on
  `SPEAKER_VERIFICATION`. Other voices, the TV and YouTube are then ignored. A neural voice-print model
  (WeSpeaker ResNet34, 27 MB, SHA-256 pinned) runs locally, and your voice print stays in `data\voiceprint.npy`.
- **Talk over it** (`VOICE_INTERRUPT`, GPU): say "stop" or "থামো" while it speaks, or just give the next
  command. Its own voice is recognised (by what it's saying, and by your voice print) and ignored.

**Faster**

- **Next sentence ready early:** the next sentence is synthesized while the current one plays, so there
  are no gaps between sentences.
- **Live transcript:** on a GPU, what you're saying appears live under the reactor.
- **Timing row:** the SYSTEM panel shows where the time went: 🎤 recognition · 🧠 AI until the first words ·
  🔊 until you heard it.
- **Fast model (optional):** set `AI_FAST_MODEL` (e.g. `llama3.2:1b`) for short everyday requests;
  `AI_MODEL` answers the harder ones ("explain", "write", "summarize"...).

**Memory and your documents** (local embeddings: `ollama pull nomic-embed-text`)

- **Conversation memory** (`CONVERSATION_MEMORY`): conversations are saved on this PC, with passwords,
  keys and card numbers removed. After a restart Jarvis continues where you left off, and "what did I say
  about my thesis last week?" searches by meaning. "forget our conversation history" deletes it.
- **Your documents:** set `DOCUMENT_FOLDERS` (e.g. `D:\Thesis`), say "index my documents", then ask
  "what's the deadline in the tender PDF?" It works with PDF, Word (.docx), text, Markdown and code, and
  the answer names the file it came from.

**Office:** "what's on my calendar today?", "read my latest emails", "send an email to ...". This uses
the classic Outlook desktop app. Sending always asks first, and email text is treated as data, never
as instructions.

**Desktop**

- **Setup wizard:** runs on first start, and any time from the menu. It covers your name, city,
  language and voice (with a test), a microphone test, AI model downloads with progress, and features.
- **Mini orb** (`MINI_ORB`): a small always-on-top reactor in the screen corner while the window is
  hidden. Double-click it to open Jarvis.
- **Presence (opt-in, `PRESENCE_DETECTION`):** the webcam notices when you walk away (after
  `PRESENCE_AWAY_MINUTES` it locks the PC) and greets you when you're back. Frames are checked for a
  face on this PC and are never saved, and CAMERA ACTIVE is shown while it runs.

**Engineering**

- **Installer:** `packaging\build.ps1 -Installer` builds `dist\JarvisSetup-<version>.exe` with Inno
  Setup: per-user, with Start Menu and desktop shortcuts, an optional start-with-Windows, and an
  uninstaller. With `JARVIS_SIGN_CERT` / `JARVIS_SIGN_PASSWORD` set, the programs and the installer are
  code-signed, so SmartScreen trusts them.
- **Updates:** the build also writes `dist\update.json` (version, url, sha256). Publish it and set
  `UPDATE_MANIFEST_URL`: Jarvis then says when an update is available, and installs it only when you
  agree, after checking the download's SHA-256.
- **Continuous integration:** `.github/workflows/ci.yml` runs ruff, mypy (informational) and the tests
  on Windows for every push. A `v*` tag builds the installer and a GitHub release.
- **Plugins** (`PLUGINS_ENABLED`, off): drop a Python file with `TOOLS` into `data\plugins`. Each plugin
  tool keeps its own permission level, and a broken plugin is skipped. Only add code you trust.
- **Settings upgrades:** `SETTINGS_VERSION` in `.env`. Old settings files are upgraded automatically,
  keeping your comments, and a `.env.bak` copy is saved.
- **Diagnostics:** Menu → *Export diagnostics* saves a zip with the version, the checks, your settings
  (secret values removed) and the logs (redacted). Conversations, memories and documents are never
  included.

### Bangla + English, and a human voice

- **Speak either language, or mix them.** With `VOICE_LANGUAGE=auto` and `VOICE_LANGUAGES=en,bn`,
  speech recognition detects the language of each sentence. It only chooses between the languages you
  list, so accented Bangla isn't taken for Hindi. Jarvis answers in the language you used: Bangla
  (Bengali script) or English.
- **Bangla commands run directly,** just like the English ones: "ক্রোম খোলো", "ফোন লিংক বন্ধ করো",
  "এখন কয়টা বাজে?", "পিসি লক করো", "ভলিউম বাড়াও", "গান থামাও", "আবহাওয়া কেমন", "ডেস্কটপের সব ফাইল গুছিয়ে দাও",
  "রিসাইকেল বিন খালি করো". Romanized Bangla works too ("chrome kholo", "koyta baje"). The answer comes
  back in Bangla. You can also say "জার্ভিস, তুমি আছো?"
- **Natural voice:** `TTS_ENGINE=neural` uses Microsoft's neural voices. Each sentence gets the voice of
  its language: `TTS_NEURAL_VOICE_EN=en-GB-RyanNeural` and `TTS_NEURAL_VOICE_BN=bn-BD-PradeepNeural`
  (`bn-BD-NabanitaNeural` is a female voice). The delivery follows the mood of the sentence: brighter
  for good news, softer for apologies. This needs the internet, and the reply text is sent to
  Microsoft's speech service. Offline, the Piper or Windows voice takes over; Bangla is then shown
  but not spoken.
- **Personality:** replies are warm and human, with a little wit, and vary their wording, instead of
  "Task completed".
- **AI model:** for good Bangla, use a multilingual *instruct* model, for example
  `ollama pull qwen3:4b-instruct` with `AI_MODEL=qwen3:4b-instruct` (llama3.2:3b is weak in Bangla).
  Avoid "thinking" models such as plain `qwen3:4b`: they reason for a minute before every answer.
  Jarvis hides that reasoning, but it can't make it faster.

### Desktop, Recycle Bin and news

- "organize my desktop" moves loose files into `Organized Desktop\Images`, `Documents`, `Videos`,
  `Music`, `Archives`, `Installers`, `Code` and `Others`. Shortcuts, folders and hidden files stay.
  "undo organize" puts every file back.
- "empty the recycle bin" always asks first, showing how many items and how much space, because the
  files are deleted permanently. "empty the recycle bin and shut down my PC" works as one command.
- "what's the news" and "news about cricket" read Google News headlines for `NEWS_REGION` (BD). Ask
  in Bangla to get Bangla headlines.

### Everyday assistant (weather, music, briefing, alerts)

- **Weather:** "what's the weather", "will it rain tomorrow", "weather in Chittagong". The data comes
  from Open-Meteo (free, no account or key; only the city is sent). Set your **Home city**
  (`HOME_LOCATION=Dhaka`) in Settings → General, and choose `TEMPERATURE_UNIT` (celsius or fahrenheit).
- **Music and video:** "pause", "play", "next song", "previous track", "stop the music". These use
  the Windows media keys, so they work with Spotify, YouTube in the browser, VLC and most players.
- **Volume:** "volume up", "louder", "mute", "set volume to 40".
- **Briefing:** "good morning" or "brief me" gives a greeting, the time and date, the weather, the
  battery, and today's reminders.
- **Proactive alerts** (`PROACTIVE_ALERTS=true`): Jarvis speaks up when the charger is plugged in or
  out, when the battery drops to `LOW_BATTERY_PERCENT` (20% by default), and again at 10%.
- **Interrupting** (`BARGE_IN=true`): say "Hey Jarvis" while it is talking. It stops at once, cancels
  the answer it was working on, and listens for your new command. While it speaks, the wake phrase
  has to be clearer than usual, so its own voice doesn't trigger it. A pending yes/no question is
  never answered this way.
- The SYSTEM panel shows **GPU** load, memory and temperature on NVIDIA GPUs.

### Direct commands

"Open X" and "close X", where X is an app the assistant can find, run the tool **directly**
without asking the AI model. It's faster, and the model can't pick the wrong tool. The call still
passes validation, permissions and the audit log. Anything else (misheard names, files,
websites, longer sentences) goes to the model as usual. Turn it off with `DIRECT_COMMANDS=false`.

These also run directly, without waiting for the model:

- **Several commands at once:** "open Chrome and Spotify", or "close Notepad, then lock my PC".
  If any part needs the model ("open Chrome and search for cats"), the whole request goes to the
  model.
- **"It" and "that":** "close it", "open that again" and "minimize it" mean the app used last. The
  model is also told what Jarvis did earlier in the session, so it understands follow-ups too.
- **Windows:** "minimize Chrome", "maximize VS Code", "switch to WhatsApp", "show the desktop".
- **Instant answers:** "what time is it", "what's the date", "how much battery do I have".

The **Response** row in the SYSTEM panel shows how long the last answer took, and whether it was
direct or needed the AI. The AI model is loaded in the background when Jarvis connects and again
when you say "Jarvis", so the first answer doesn't wait for the model to load
(`OLLAMA_KEEP_ALIVE=30m` keeps it loaded between requests).

**Speech hints:** the recognizer is told the wake phrase, the assistant's name, and common app
names (WhatsApp, Chrome, VS Code, Spotify...), plus the apps in `config/apps.json`. To improve
recognition of an app you use often, add it to `apps.json`.

**How apps are found:** first `config/apps.json` aliases ("browser" → Chrome), then the
Windows App Paths registry, PATH and standard install folders. Anything else is found by
searching Start Menu shortcuts and the Windows Apps folder (which also lists Microsoft Store
apps such as WhatsApp), so most installed apps work without configuration. Check how a name
resolves with `python run.py --find-app whatsapp`.
Uninstaller shortcuts are never matched. To add a nickname, add it to `aliases` in
`config/apps.json`. After launching, the assistant checks that the app's process actually
started, and says so if it can't confirm.

**Chrome and Edge profiles:** "open Chrome" opens the profile you used last, straight to a new
tab, without "Who's using Chrome?". To always use one profile, set `CHROME_PROFILE` (or
`EDGE_PROFILE`) to its name, for example `CHROME_PROFILE=Your Chrome`, in Settings → Browser.
`none` brings the picker back. You can also say "open Chrome with my Night profile".
`python run.py --browser-profiles` lists your profiles and shows which one will open.

**Websites and web apps:** "open YouTube / Facebook / Instagram / WhatsApp..." opens the app if it
is installed on this PC (WhatsApp, Spotify, Telegram, Outlook...). Otherwise it opens the website in
`WEB_BROWSER` (default: your Windows default browser; `chrome`, `edge` or `firefox`), in the same
profile as above. "open YouTube in Edge" picks the browser. Only http(s) addresses are opened.

**Lock:** "lock my PC / laptop / computer / screen" shows the Windows lock screen right away,
without asking the AI model. It's also recognised when speech recognition writes "block my PC".

**Restart and shut down:** "restart my PC", "shut down the computer", "turn off my laptop".

- Jarvis counts down 10 seconds first. Say "cancel", press Esc, or click Stop to abort.
- After the countdown Windows restarts normally. An app with unsaved work can still ask you; nothing is
  force-closed. (Jarvis runs the countdown itself, because `shutdown /t N` would force-close apps
  without saving.)
- Open work is saved first (`SAVE_BEFORE_POWER=true`, the default). Jarvis presses Ctrl+S in every
  window whose title shows unsaved changes (● or \*, as in VS Code and Notepad). Never-saved tabs in
  Notepad and VS Code are fine, because they come back after the restart. Any other never-saved file
  needs a name, so Jarvis doesn't restart and tells you which window needs you.
- `CONFIRM_POWER_ACTIONS=false` (Settings → Privacy) skips the dialog when **you** asked for it: a direct
  command, or any message of yours with the word "restart" / "shut down" / "turn off" (so a misheard
  "restart my computer bomb" works too). When the AI decides to restart on its own, for example because of something on a web page,
  it always asks. Signing out always asks.
Protected processes (`protected_processes` in `config/permissions.json`, plus core
Windows processes that are always protected) can never be ended.

### Adding a tool

```python
# app/tools/example.py
from pydantic import Field
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult

class GreetArgs(ToolArgs):                      # extra fields are rejected automatically
    name: str = Field(min_length=1, max_length=40, description="Who to greet")

def greet(args: GreetArgs, ctx: ToolContext) -> ToolResult:
    if args.name.lower() == "nobody":
        raise ToolError("There is nobody to greet.", "invalid")   # shown to the user
    return ToolResult.success(f"Greeted {args.name}.", name=args.name)

TOOLS = (
    Tool(name="greet", description="Greet someone by name.", handler=greet,
         args_model=GreetArgs, level=RiskLevel.SAFE, timeout=5.0),
)
```

Then add `example` to `TOOL_MODULES` in `app/tools/catalog.py` and write tests (see
`tests/test_tools.py`). Rules: never pass model-provided text to a shell; give side-effect
tools a correct `level` and a clear `confirmation` question; raise `ToolError` for expected
failures.

## Run the tests

```powershell
python -m pytest -q                              # everything that runs anywhere (about 850 tests)
python -m pytest tests/test_windows_real.py -v   # real Windows APIs, screen capture and OCR (read-only)
```

`docs/TESTING.md` has the opt-in real-hardware tests (Notepad, hotkeys, Ollama, Edge) and a
10-minute hands-on checklist for voice, confirmations and privacy indicators.

## Configuration

| Where | What |
|---|---|
| `.env` | Environment settings: AI provider, Ollama host, model, wake word, language, logging. Real environment variables take priority. |
| `config\apps.json` | Applications and their spoken aliases ("browser" -> Chrome). There are no personal install paths; apps are found through Windows App Paths, PATH, `%ENV%` candidates or URI handlers. |
| `config\permissions.json` | Confirmation rules, protected processes, safe folders, allow-listed dev commands. |
| `config\shortcuts.json` | Global hotkeys. |

A config file you delete is recreated with defaults. A config file with an error
stops startup and the message names the file, the line and the field. The
assistant never falls back to defaults behind your back.

Runtime data (`data\assistant.db`, `data\logs\jarvis.log`) is created on first run
and ignored by git. To keep it elsewhere, set `JARVIS_DATA_DIR`. The packaged
`JARVIS.exe` stores it in `%LOCALAPPDATA%\JarvisAssistant`.

## Project layout

```
app/
  main.py            bootstrap + CLI (--doctor, --models, --chat, --root, --version)
  cli.py             terminal model list and text chat
  ai/                base.py (provider interface), ollama.py, manager.py (model selection), prompts.py
  agent/             context.py, conversation.py (agent loop), tool_manager.py (registry + executor), tool_router.py
  tools/             base.py, basic.py, applications.py, windows.py, system.py, filesystem.py, developer.py, memory.py, browser.py, automation.py, vision.py, winapi.py, catalog.py
  security/          permissions.py, confirmations.py, validator.py (paths), command_policy.py, command_executor.py, rate_limit.py
  voice/             audio.py, vad.py, microphone.py, speech_to_text.py, wake_word.py, pipeline.py
  tts/               base.py, piper.py, sapi.py (Windows voice), player.py, manager.py (speech queue), text.py
  cli_voice.py       terminal voice commands
  browser/           search.py (web search), playwright_manager.py (the assistant's browser)
  automation/        triggers.py, tasks.py (steps + SQLite store), scheduler.py
  vision/            capture.py (screen/window capture, blocked windows), ocr.py (Windows OCR, Tesseract)
  ui/                app.py, main_window.py, controller.py (GUI <-> assistant), visualizer.py, chat.py,
                     confirm_dialog.py, tray.py, hotkeys.py, settings_dialog.py, memory_dialog.py,
                     log_viewer.py, automation_dialog.py, styles/theme.py
  config/            settings.py (.env), env_writer.py (safe .env saving), files.py (JSON configs)
  memory/            database.py (SQLite + migrations), manager.py (long-term memory), settings_store.py, conversations.py, audit.py
  utils/             paths.py, logging_setup.py, redaction.py, async_runner.py, autostart.py
config/              apps.json, permissions.json, shortcuts.json
tests/               pytest suite
run.py               launcher
```

## Security basics (already in place)

- Logs are JSON lines, and every record is redacted before it reaches disk: API
  keys, bearer tokens, `password=...`, card numbers, private keys and URL credentials.
- Launch values in `apps.json` and dev-command tokens reject shell metacharacters
  (`& | ; < > ^ " \``). Dev commands are argv lists, never shell strings.
- Safe roots must be absolute paths or known-folder tokens, and cannot contain `..`.
- Permission overrides can only raise a tool's risk level. This is enforced in Phase 7.

## Adding an AI provider

1. Create `app/ai/<name>.py` with a class that implements `AIProvider` from `app/ai/base.py`:
   `health()`, `list_models()`, `chat()`, and optionally `model_capabilities()`,
   `stream_chat()` and `aclose()`. Convert to and from the neutral types
   (`ChatMessage`, `ToolSpec`, `ToolCall`, `ChatResponse`). Raise the `AIError`
   subclasses with messages a user can understand.
2. Register a factory in `PROVIDERS` in `app/ai/manager.py`.
3. Add the name to `SUPPORTED_AI_PROVIDERS` in `app/config/settings.py`, plus any
   settings it needs. Read API keys only from environment variables; they are
   redacted from logs automatically.
4. Add tests with a fake HTTP transport, following `tests/fake_ollama.py`.

## Troubleshooting

| Problem | Fix |
|---|---|
| `python` opens the Microsoft Store or is "not recognized" | Install Python from python.org with "Add to PATH" ticked, or turn off the `python.exe` alias under *Settings > Apps > Advanced app settings > App execution aliases*. You can also use `py -3.12` instead of `python`. |
| `Activate.ps1 cannot be loaded because running scripts is disabled` | Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or use CMD with `activate.bat`. |
| "Text recognition isn't available" / "no OCR language" | Run `pip install -r requirements.txt` for the Windows OCR packages. Then check that the language pack includes OCR: *Settings > Time & language > Language & region > your language > Language options > Optical character recognition*. `python run.py --screen-test` lists the installed OCR languages. |
| `describe_screen` only reads text | No vision model is installed. Run `ollama pull qwen2.5vl:3b`, or set `VISION_MODEL`. |
| `.env has line(s) that are not in NAME=value form` | Something that isn't a setting was pasted into `.env` (a PowerShell command, for example). Fix or delete the line number shown. |
| "I couldn't find X on this computer" | Check the app is in the Start Menu. Otherwise add it to `config/apps.json` with `launch: {"type": "executable", "value": "C:\\Path\\App.exe"}`. |
| Volume commands fail | Run `pip install -r requirements.txt` (needs `pycaw`). The tool falls back to the volume keys if the audio API fails. |
| "Close X" says it isn't open, or the window stays open | Run `python run.py --windows` while the window is on screen and send me the line for that app. It shows how Windows reports each window (A = Alt+Tab window, T = tool window, C = cloaked). |
| Chrome opens to "Who's using Chrome?" | That's Chrome's profile picker. Untick "Show on startup" in it, or pick a profile once. |
| "Windows did not allow switching to X" | Windows blocks focus changes from background apps. Click the terminal first, or use the app's taskbar button. |
| The model answers instead of using a tool, or writes JSON | Small models are hit-and-miss with tools. JSON written as text is recovered automatically. For more reliable tool use, try a larger tool-capable model (for example `qwen3:8b`) if your hardware allows. |
| `Configuration problem: ...` at startup | Fix the value named in the message in `.env` or the named JSON file. Deleting a JSON config file restores its defaults. |
| `Storage problem: ... newer version` | That database was created by a newer build. Update, or set `JARVIS_DATA_DIR` to another folder. |
| `Ollama is not running. Start Ollama and try again.` | Start Ollama from the Start menu, or run `ollama serve`. Check with `curl.exe http://127.0.0.1:11434/api/version`. If you changed `OLLAMA_HOST`, the app and Ollama must use the same address. |
| `No chat model is installed in Ollama` | Run `ollama pull <model>` (see Ollama setup), then `ollama list`. |
| `The model 'x' (AI_MODEL in .env) is not installed` | Fix the name in `.env` to match `ollama list`, or leave it empty for automatic selection. |
| Replies are very slow | The model is too big for your RAM/VRAM. Try a smaller one, and leave `OLLAMA_KEEP_ALIVE=10m` so it stays loaded between requests. The first reply after a load is always slower. |
| `The AI model took too long to respond` | Use a smaller model or raise `AI_REQUEST_TIMEOUT`. |
| `--mic-test` says "No sound detected" | Open *Settings > System > Sound > Input*, choose the right microphone and check that it isn't muted. Also check *Settings > Privacy & security > Microphone*: "Let desktop apps access your microphone" must be on. Or set `MICROPHONE_DEVICE` to a number from `--devices`. |
| `Could not open the microphone` | Another app may be holding it exclusively (a call app, for example). Close that app, or pick the device's MME entry from `--devices`. |
| `The speech model ... is not downloaded yet` | Run `python run.py --setup-voice` while online. |
| `could not use the GPU (CUDA 12 / cuDNN 9 ...)` | Set `STT_DEVICE=cpu`, or install the NVIDIA CUDA 12 and cuDNN 9 libraries. |
| Recording cuts off mid-sentence, or waits too long | Raise or lower `SILENCE_TIMEOUT` (seconds). |
| App names are misheard ("close bar set" instead of "close WhatsApp") | Keep `STT_MODEL=auto`; on an NVIDIA GPU, install `requirements-gpu.txt` so large-v3-turbo runs (the SYSTEM panel shows the model in use). Try the laptop's **Microphone Array** (`MICROPHONE_DEVICE=Microphone Array`) and compare with `--mic-test`. Speak the app name clearly after a short pause. |
| It often "didn't catch that" | Use `small.en`, get closer to the mic, or lower `STT_MIN_CONFIDENCE` slightly. |
| No voice output | Run `python run.py --say "test"` to see the engine error. Check the speaker selected in Windows, or set `OUTPUT_DEVICE`. |
| `pip install` fails on `piper-tts` or `faster-whisper` | Use 64-bit Python 3.11-3.13 and upgrade pip first. |
| `python run.py` says "The desktop app could not start" | Run `pip install --force-reinstall PySide6`. Meanwhile `--chat` and `--voice` still work. |
| "These hotkeys are used by another program" | Pick other combos in `config/shortcuts.json`, e.g. `"toggle_listening": "ctrl+alt+j"`. |
| The window disappeared | It's in the system tray (click ^ next to the clock). Ctrl+Space brings it back. |
| `PermissionError` on the log file | Another program (an editor or log viewer) has `jarvis.log` locked during rotation. Close it. |

## Roadmap

1. Foundation (done)
2. Local AI: Ollama provider, model detection, conversation manager (done)
3. Voice: microphone, faster-whisper STT, Piper TTS, push-to-talk (done)
4. Tool engine: registry, structured calls, executor (done)
5. Windows: apps, windows, power, volume, system monitor (done)
6. Filesystem tools with path validation (done)
7. Security: permission levels, confirmations, command policy, rate limits (done; GUI dialogs in Phase 9)
8. Memory: long-term memory and conversation context (done)
9. PySide6 GUI, tray, hotkeys, settings, log viewer, memory manager (done)
10. Browser automation (Playwright) (done)
11. Automation, scheduled tasks and multi-step workflows (done)
12. Vision: screenshots and OCR (opt-in) (done)
13. Full test pass (done)
14. `JARVIS.exe` packaging, installer and HUD interface (done)
