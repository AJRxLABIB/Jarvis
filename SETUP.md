# Jarvis Assistant - Full Setup (Windows 11)

Local-first AI assistant: Python 3.11+ (3.12 recommended), PySide6 desktop UI, Ollama language model, local voice
(faster-whisper, Piper / Kokoro / Edge TTS). Version 3.25.0 plus the task-engine, retrieval, to-do, developer-tool and
backup upgrades (see `docs\task-engine.md`).

## 1. Prerequisites

1. Windows 11, 64-bit.
2. Python 3.12 (3.11 works) from python.org - tick **Add python.exe to PATH**.
3. Ollama from https://ollama.com/download (listens on http://127.0.0.1:11434).
4. Microsoft Edge (browser automation) and a microphone for voice.
5. Optional: NVIDIA GPU + current driver for fast speech recognition. Optional: Git, VS Code, Node (developer tools).

## 2. Python environment

```powershell
cd D:\Jarvis
python -m venv .venv
.venv\Scripts\Activate.ps1          # CMD: .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-gpu.txt # optional, NVIDIA only (~1 GB)
```

If PowerShell blocks activation: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
Your existing `.venv` already works; only redo this on a new PC.

## 3. Settings

```powershell
copy .env.example .env     # skip if you already have a .env - it is never overwritten
notepad .env
```

Main values: `AI_MODEL`, `OLLAMA_HOST`, `WAKE_WORD`, `USER_ADDRESS`, `VOICE_LANGUAGE(S)`, `STT_DEVICE`, `TTS_ENGINE`,
`DEV_PROJECT_DIR`, `DOCUMENT_FOLDERS`, `HOME_ASSISTANT_URL` / `HOME_ASSISTANT_TOKEN` (only for Home Assistant).
Keep `.env` private. Never put it in a zip, a repository or a chat.

## 4. AI models (Ollama)

```powershell
ollama pull qwen3:8b            # or llama3.2:3b for 8 GB RAM; the model must support tool calling
ollama pull nomic-embed-text    # document and history search (meaning search)
ollama list
python run.py --models
```

Optional role models: `AI_FAST_MODEL`, `AI_SMART_MODEL`, `AI_CODING_MODEL` (e.g. `qwen2.5-coder:7b`), `VISION_MODEL`.
A model that fails 3 times in a row is skipped for a while; a model bigger than your RAM is never tried.

## 5. Voice models (about 700 MB, stored in `data\models`)

```powershell
python run.py --setup-voice
python run.py --devices
python run.py --mic-test
```

## 6. Check and run

```powershell
python run.py --doctor      # diagnostics (Python, database, AI, voice)
python -m pytest -q         # full test suite
python run.py --chat        # terminal chat
python run.py --voice       # push-to-talk
python run.py               # desktop app
python run.py --background  # optional: windowless host for scheduled automations (opt-in logon task: packaging\background.ps1)
python run.py --rollback-update   # after a bad update: restore the pre-update data backup (reinstall the old program yourself)
```

The first start upgrades the database to schema v35 and writes a copy first (`data\assistant.v<N>.bak.db`).

## 7. Backup and restore

```powershell
python run.py --backup-db                     # verified snapshot in data\backups (newest 7 kept)
python run.py --test-restore                  # restore the newest backup into a temp copy and check it
python run.py --voice-doctor                  # test every microphone (3 s each) and name the best one
python run.py --export-tokens                # print the UI design tokens as JSON
python run.py --wake-doctor                   # say "Hey Jarvis" 6 times; offers (y/N) to learn how your voice spells the name
python run.py --restore-db data\backups\assistant-YYYYMMDD-HHMMSS.db   # close Jarvis first
```

A restore checks the backup, refuses while Jarvis is using the database and keeps the current one as
`pre-restore-<time>.db`. Also copy `.env` and `data\` yourself when moving to another PC.

## 8. New abilities you can ask for

- Missions: "start a mission ...", "continue my mission", "what happened to my mission" (history), "roll back".
- To-dos: priority, category, repeat (daily / weekdays / weekly / monthly), "after" another item, postpone.
- Reminders: "remind me in 10 minutes ...", "snooze that for 15 minutes".
- "How is my day?" / "what did I complete today?" (command center; no calendar is assumed).
- Documents: index with "index my documents", ask with "what does the tender say about the deadline?" - answers cite
  `[1] file`.
- Files: "when a new PDF appears in my Downloads folder, ..." (file-change trigger); steps can run only on weekdays,
  on battery, or if the previous step worked/failed.
- "Search everything about the tender" (memories + documents + past chats, ranked), "show integrations",
  "trace my last request", "try this change in an experiment" (separate branch; merge or discard).
- "open workflow studio": check (dry run), reorder and add conditions to your workflows safely; "voice stats" shows
  how voice has been going; "export my memories", "stop using my <category> memories".
- "plan my day" (give to-dos minutes: "... 45 minutes"), "replan, I lost an hour", "review my day".
- "triage my emails" (suspicious / urgent / routine), "draft a reply ..." (never sent without asking), "keep a research
  project called ...", "quiz me on ..." (spaced repetition), "export workflow ...", "what did you change today" /
  "which changes weren't verified" (activity timeline), "what is normal for my PC".
- "resume <workflow>" continues a workflow that stopped; "security scan my project" (read-only checklist).
- `python run.py --eval-quality` scores the model's answers (including "admit you don't know"); add questions in
  `data\benchmark\quality.json`.
- `python run.py --perf` checks that commands are understood and times the main internal steps.
- Code: "what would changing <function> affect?" (code_impact), safe edits with test checks, commits that never stage
  `.env` / keys.

- **Google Calendar and Drive** (needs your own Google OAuth client, see "Google setup" below): "connect Google",
  "what's on my calendar this week", "add lunch with Rafi tomorrow at 1pm to my calendar" (asks first), "find my budget
  file in Drive" (file names only, never contents), "disconnect Google".
- **Profiles**: "switch to meeting mode" / "night" / "performance", "save my current settings as desk", "undo that profile".
  Shows what changes first; never touches secrets, paths or security settings.
- "simulate <workflow> when a step fails" or the **Simulate...** button in the workflow studio (nothing real runs);
  "troubleshoot", "sync my to-dos with my phone", "background mode status".
- `python run.py --audit-accessibility` checks the app's colours against WCAG; the test-suite checks that every control
  has a name and keyboard access.

### Google setup (once, about 10 minutes)

1. Open <https://console.cloud.google.com/>, create a project (any name).
2. APIs & Services -> Library: enable **Google Calendar API** and **Google Drive API**.
3. APIs & Services -> OAuth consent screen: user type *External*, fill the app name and your e-mail, then under *Test users*
   add your own Google account. (It stays in "Testing"; that is fine for personal use. Google may expire the sign-in every
   7 days in this mode, then just say "connect Google" again.)
4. APIs & Services -> Credentials -> Create credentials -> **OAuth client ID** -> application type **Desktop app**.
5. Copy the client ID and secret into Jarvis: Settings -> Google (Calendar, Drive), or `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` in `.env`.
6. Say **"connect Google"** (or ask Jarvis to use `google` with action connect). Your browser opens Google's own page;
   approve **calendar events** and **Drive file names**. Done; the lasting sign-in is stored encrypted for your Windows account.

## 9. Optional: install as JARVIS.exe

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Install
```

About 10 minutes; installs to `%LOCALAPPDATA%\Programs\Jarvis` and copies your settings and data on the first install.
Remove with `packaging\uninstall.ps1` (add `-RemoveData` to delete settings and memories too).

## 10. Optional: Android app

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_android.ps1
```

Downloads OpenJDK 17 and the Android SDK tools and signs with the key in `android\signing`. Keep that key: updates
must use the same one. See `docs\ANDROID.md` and `docs\PHONE_SETUP.md`.

## Known limits

- The upgrade was tested on Linux (Python 3.10, PySide6 offscreen): all tests passed there. Run `python -m pytest -q`
  on your PC and the real-hardware tests in `docs\TESTING.md`; `docs\STATUS.md` lists what is still open.
- "My day" (Menu) is the new window for to-dos, today's overview and mission history.

## Troubleshooting

- `python` not found: reinstall Python with PATH ticked.
- Ollama not answering: start the Ollama app, then `ollama list`.
- No microphone or speaker: `python run.py --devices`, then set `MICROPHONE_DEVICE` / `OUTPUT_DEVICE` in `.env`.
- Database problem: `python run.py --doctor`; restore with `--restore-db`.
- More: `README.md` ("Troubleshooting") and `docs\`.


## Optional: round 13 features (all off until you set them up; none tested on real hardware/accounts)

* **Windows service instead of the logon task** (administrator PowerShell, `.venv` created, pywin32 installed):
  `.\packaging\service.ps1 -Install` asks for your Windows password once, installs and starts "JarvisBackground" as YOUR
  account (give it "Log on as a service" if Windows refuses). `-Status` / `-Uninstall`. Remove the logon task first
  (`.\packaging\background.ps1 -Uninstall`); the script refuses to install while it exists.
* **Automatic model switching:** run `python run.py --eval-quality --model <name>` for each model you use, then set `AI_AUTO_SWITCH=true`.
  Without benchmark results it never switches.
* **Phone push (FCM):** create your own Firebase project, download a service-account key (Project settings > Service accounts)
  and set `FCM_SERVICE_ACCOUNT=C:\path\key.json`. Switch `notifications` on for the phone (`mobile_sync`). Only a content-free
  ping is sent. The bundled Android app cannot receive it yet (no Google Play services in this build); it polls instead.
* **Android to-do sync:** rebuild the app (`packaging\build_android.ps1`), reinstall, then Settings > "To-do sync". Switch
  `todos` on for the phone first (`mobile_sync`). Background checks are opt-in there.
* **Canvas:** Workflow studio > "Canvas" tab (the "Steps" tab is the screen-reader view).

## Round 15 features (say these to Jarvis)

* "what do I keep doing every morning?" / "automate <key>" / "dismiss <key>" - routine suggestions (needs a few days of use).
* "when did I last run the backup?" / "how many times did I open Spotify this week?"
* "focus for 45 minutes and keep distracting apps closed" (apps come from FOCUS_APPS).
* Clipboard memory is OFF until you tick it (Settings > Memory) or set `CLIPBOARD_HISTORY=true`. Then: "paste what I copied about
  the invoice", "what did I copy earlier", "clear my clipboard history".

## Round 16 features

* **Standing approvals:** "always allow <tool>" style grants for MODERATE tools only (never network-sending, persistent or
  never-grantable tools; max 12 h; revoked while any emergency switch is on). Say "show my approvals" / "revoke all approvals";
  listed in the Security Center.
* **Conversation memory in long chats:** when old turns are trimmed, a short redacted digest (your requests + tool names, never
  assistant or tool text) stays in context.
* News feeds that contain XML entity tricks are refused.

## Always-on personal assistant

`python run.py --always-on` - starts with Windows, goes straight to the system tray (no window) and listens hands-free for
the wake word. Open the window from the tray icon; closing the window only hides it. `python run.py --always-on-off` undoes it.
It needs a working microphone (`--voice-doctor`). Untested on real Windows start-up here.

## Round 16b

* `TAINT_LOCKDOWN=true` (Settings > AI): after Jarvis reads a web page, email or document, the AI is not even offered email,
  WhatsApp, calling, smart-home, typing or save tools in that conversation (searching still works). Off by default.
* Hands-free now tells you once if the microphone has been silent for 90 s (privacy setting / input volume / wrong device).

## Restart / refresh by voice

Say "Hey Jarvis, refresh" or "Hey Jarvis, restart yourself" (also "reload yourself", "restart Jarvis"). Jarvis closes, starts a
fresh copy of itself (in the tray if it was hidden) and loads the latest code - no PC restart. Typed commands work too.
It reloads Python code and settings from `D:\Jarvis`; "restart my PC" is unchanged. Untested on real Windows here.

## The 3D core (round 18)

The centre of the dashboard is a real-time 3D scene drawn by your graphics card (OpenGL shaders through `moderngl`):
a particle nucleus and shell, neural filaments with light pulses, tilted orbital rings, and a halo with a scan line.
It has states (ready, listening, processing, executing, speaking, waiting for approval, error) that ease smoothly into each other.
Install the new dependency once: `pip install -r requirements.txt` (adds `moderngl`).
Settings (Settings > General): 3D core `auto|gl|painter`, 3D quality `auto|low|balanced|high`, animation speed, glow, reduced motion.
`auto` quality lowers itself (never raises) if frames are slow. If OpenGL or moderngl is missing or fails, Jarvis keeps running
with the flat painter-drawn core. Drag the core to inspect it; it glides back when you let go.

## Window and tab overview / screen reading

* "Show me my screen", "show my windows", "mission control" (or MENU > Show all windows): every open window flies into a
  grid with its live picture; click one (or arrows + Enter) and it zooms back and comes to the front; Esc closes.
* "Show my tabs" / "show my chrome tabs" (or MENU > Show browser tabs): every browser tab as a card; pick one to switch to it.
  The live pictures are drawn by Windows itself - nothing is captured or sent anywhere.
* Reading/seeing the screen ("read this", "what's on my screen", "can you see my screen", "what am I looking at") needs
  screen capture to be allowed: Settings > Privacy > Allow screen capture (or say "turn on screen capture").
  Descriptions of pictures/layout need a vision model: `ollama pull qwen2.5vl:3b` (text reading works without one).

## Talking over Jarvis / a more human voice

* While Jarvis speaks you can simply start talking: within about a quarter of a second it goes quiet and listens, then
  answers what you said. If it was only a cough or the TV, it carries on from the sentence it stopped at. "Stop" makes it
  quiet and listen. (Hands-free on, speech recognition on the GPU; `VOICE_INTERRUPT=true`.) Headphones or a headset make
  it most reliable; with loud speakers right next to the microphone speak a little louder than Jarvis.
* The voice now uses a natural, professional finish (no robotic echo effect) at the voice's own pitch. Settings > Voice
  output > Voice finish = classic brings the old synthetic edge back (`TTS_STYLE=classic`).

## Autonomy levels, duplicates, batch rename, expenses (round 26)

* Settings > Privacy (security) > "How much Jarvis may do by itself" (`AUTONOMY`):
  **observe** (only reads and answers), **suggest** (plans: it tells you each step it WOULD take - "Suggested, not
  done: ..." - without doing it), **routine** (default: low-risk actions run, risky ones ask), **approval**
  (everything that changes something asks first; no standing approvals). Narrower limits on top:
  "... for requests from my phone" (`AUTONOMY_REMOTE`, the stricter level applies), "Always ask for these kinds of
  actions" (`AUTONOMY_ASK_CATEGORIES`, e.g. `files, phone`) and "Never do these kinds of actions"
  (`AUTONOMY_BLOCK_CATEGORIES`, e.g. `home`). Categories are listed in docs/CAPABILITIES.md. They apply at once.
* Approving from the phone needs the phone's fingerprint / PIN key (saying NO never does). Standing "don't ask me
  again" approvals made on the PC never cover requests that come from a phone. The emergency stop revokes them at
  once and tells you what it stopped and what it could not interrupt.
* Microphone mute: **Ctrl+M**, MENU > "Mute microphone", the tray menu, or say "mute the microphone". While it's on
  NOTHING listens (no wake word, no push-to-talk) and the microphone stream is closed; a red MIC MUTED badge shows.
  A cyan TRANSCRIBING badge shows while a spoken command is being turned into text.
* Memory: "change that memory to ...", MENU > Memory... > Edit (the old wording stays in its history);
  "import my memories" (the newest export in data\exports, or name the file; validated, nothing is deleted);
  "why did you mention that?" (which saved memories the last answer used, and why).
* Reminders missed while Jarvis was closed - or while the PC was asleep - are reported ("Missed ... skipped, not
  run late"), never fired in a burst and never dropped silently.
* If Jarvis has to use a different model than the one you chose (it can't use tools, is too big, keeps failing),
  the answer says so - once per reason.
* Phone: "Cancel what Jarvis is doing" stops the current request (the emergency stop is still there for everything).
* `python run.py --capabilities` (and docs/CAPABILITIES.md): what works offline / on your network / with internet /
  with credentials, and every tool's risk.
* Sources: under an answer that used tools or saved memories, a line says where its facts came from
  ("open_application (worked, checked) · 1 saved memory · everything else is the AI's own wording"). Ask "where did
  you get that?" for the full record. Settings > "Show where an answer's facts came from" (`SHOW_SOURCES`).
* MENU > **AI and models...**: every installed model with what Ollama says it can do (tools, vision, thinking,
  embedding), whether it fits this PC's memory, how it behaved here, and your benchmark score. Recommended model per
  job (general / fast / smart / coding / vision / embedding) from those facts only - without a benchmark it says so
  instead of guessing. "Apply recommended roles" asks first. Same in a terminal: `python run.py --model-roles`
  (add `--apply` to save after a y/N question). Run `python run.py --eval-quality` first for scores.
* MENU > **Device manager...**: this PC and other computers, paired phones (battery, last check-in, whether the phone
  allows control; Revoke) and smart devices on your network (Check now, Add, Remove).
* Smart devices on your network without Home Assistant: Shelly (Gen 1 and Gen 2+), Tasmota and WLED - add them in
  the Device manager (name, type, IP address). "turn on the desk lamp", "set the desk light to 40 %",
  "is the heater plug on?". Only private network addresses are accepted, devices with a password aren't supported,
  plugs ask before switching (you can untick that per device), and a change is reported as done only when the
  device itself confirms its new state. Home Assistant changes are now confirmed the same way.
* "find duplicate files in Downloads" - identical files by content and the space they waste (never deletes).
* "rename all the jpgs in Pictures\Trip: replace IMG_ with Trip-" - shows a preview first; "do it" renames, checks for
  clashes first, and "undo that" gives the old names back.
* "I spent 450 taka on lunch", "how much did I spend this month?", "set my food budget to 8000" - local expense records
  and monthly budgets (database v33; no bank connection).
  "export my expenses" writes a CSV (formula-safe) to data\exports; "expense trend" = last 6 months.
* Journal / voice notes: "note to self: call the bank", "add finished chapter 3 to my journal", "what did I write
  today?" (spoken entries are marked as voice notes). Habits: "track the habit drink water", "mark habit exercise
  done", "which habits are left today", "my habit streaks". "Any calendar conflicts this week?" checks Outlook for
  overlapping appointments.
* MENU > My life log: journal, habit grid (click a day to tick it), expense charts + CSV export, calendar conflicts.
  MENU > Knowledge... > Browse: a map of your projects, files, decisions, memories and goals - click to walk the links.
* `python run.py --restart` (in the Jarvis folder): restarts the running Jarvis, or starts it if it isn't running.
  The window restarts itself within ~2 s; if it doesn't answer in 20 s it is stopped (only if the process really
  is Jarvis) and started again.
* Phone link (JARVIS Mobile, rebuild the app first: `packaging\build_android.ps1`): on the phone, Settings >
  "Let my PC control this phone" (off by default) allows "turn on my phone's flashlight", "ring my phone",
  "open WhatsApp on my phone", "open this link on my phone", "phone battery". "Forward notifications to my PC" +
  "Give notification access" sends phone notifications to the PC's notification list (one-time codes are hidden).
  Share > JARVIS on the phone sends text/links to the PC. Actions expire after 2 minutes.

## Jarvis learns from every request and improves itself (with proof)

* **Learning from use** (`LEARN_FROM_USE=true`, on by default; Settings > "Learn from every request"): each request
  is recorded on this PC (redacted) with the tools it used. Whether it went right is decided by what you do next -
  a failed step, "no", "wrong", "I meant ..." or asking the same thing again = it went wrong; moving on or 2 quiet
  minutes = it went right. Never in private mode, never for automations.
* From that it learns **skills** (requests like this -> these tools worked) and **lessons** (a mistake and what you
  wanted instead). Similar ones are shown to the AI for new requests. Each one is scored by the results of the
  requests it was shown for; ones that don't help are retired automatically.
* "what have you learned?" / "are you getting better?" - success rate week by week, skills, lessons.
  "show your lessons", "forget what you learned" (asks first; memories are not touched).
* **"tune your AI"** / "make yourself smarter" (or `python run.py --self-improve`): Jarvis tests set-ups for its own
  brain - each installed tool-capable model that fits your PC, and a more exact setting - on a test built from YOUR
  real requests (the right tool is known from what worked). It switches only when the result is clearly better
  (+5 % right, or the same score and 20 % faster) and never to one that makes more broken tool calls. It needs at
  least 8 of your requests with a known answer first. Every run is logged in data\self_improve.jsonl.
* "improve yourself" still looks at what keeps failing and drafts code changes - tested on a copy and applied only
  after you approve. It now also reports how the learning is going.
* `python run.py --export-training` writes your good requests (and the answers you liked) as chat JSONL to
  data\exports\training.jsonl for an OPTIONAL fine-tune with an external trainer (needs a strong GPU; not included).

## Full power (FULL_POWER=true - switched on for you)

Nothing is off-limits any more. Actions that used to be refused outright (formatting or wiping drives, Windows and
Program Files folders, deleting whole folders, ending protected system processes, system tools such as PowerShell,
reg, diskpart, bcdedit, and changes to Jarvis's own security code through the Evolution lab) now ASK YOU instead,
in a dialog marked CRITICAL. Rules that stay, so the power stays yours:
* every critical action asks, every time, for that exact action - a "yes" is never reused;
* it must be answered on the PC's screen: not by voice (Jarvis says "please answer on the screen"), not from a phone,
  never by a standing approval or a mission plan;
* only for requests you make at the PC (typed or spoken) - never for phone requests, automations or agents;
* hidden instructions in web pages / emails / documents still can't approve anything (they trigger a question);
* the emergency stop (Ctrl+Shift+F12) still stops everything; the secrets file (.env) is never changed by Jarvis;
* switch it off any time: Settings > Security > "FULL POWER" (applies at once).
