# Testing Jarvis

There are three layers: the automated suite (runs anywhere), real-Windows tests
(run on your PC), and a short hands-on checklist for what only a person can
judge (voice, dialogs).

## 1. Automated suite

```powershell
python -m pytest -q
```

It covers every tool, the permission engine, path and command policies, memory,
automation, the GUI (off-screen), and the voice pipeline with fake audio. It also
runs whole-codebase security checks (`tests/test_security_static.py`):

- no `shell=True`, `os.system`, `eval`/`exec` or pickle anywhere in `app/`
- only five reviewed modules may start processes, and always with argument lists
- the agent and AI layers never start processes
- every tool has strict arguments (unknown fields rejected), a description for each
  field, a router category, a timeout, and is not critical
- every setting is documented in `.env.example`, and privacy features default to off

Coverage report (optional): `pip install pytest-cov`, then
`python -m pytest -q --cov=app --cov-report=term-missing`.

## 2. Real Windows tests

`tests/test_windows_real.py` uses the real Windows APIs: known folders, the window
list, app lookup, volume, system stats, Windows voices, the Start-with-Windows
command, git, real screen capture of a test window, and real Windows OCR. They
are read-only. A small test window flashes up for about 3 seconds.

```powershell
python -m pytest tests/test_windows_real.py -v
```

Opt-in extras:

```powershell
# opens and closes Notepad, registers a spare hotkey, toggles Start with Windows and restores it
$env:JARVIS_TEST_INTERACTIVE = "1"
# asks your installed Ollama model two questions (needs Ollama running)
$env:JARVIS_TEST_OLLAMA = "1"
# drives Edge in the assistant's own profile
$env:JARVIS_TEST_BROWSER = "msedge"
python -m pytest tests/test_windows_real.py tests/test_browser_real.py -v
Remove-Item Env:JARVIS_TEST_INTERACTIVE, Env:JARVIS_TEST_OLLAMA, Env:JARVIS_TEST_BROWSER
```

## 3. Hands-on checklist (about 10 minutes)

Start the app with `python run.py`. Tick each line.

**Startup**
- [ ] `python run.py --doctor` shows no `fail` lines
- [ ] The window opens; the status shows the Ollama model; the tray icon appears
- [ ] Starting a second copy says "already running"

**Voice**
- [ ] Press Talk and say "what time is it": MICROPHONE ACTIVE shows while you speak, and the answer is spoken
- [ ] "Hey Jarvis" (voice mode, `--voice`) wakes it
- [ ] Esc while it speaks stops the speech
- [ ] ☰ → Always listening: HANDS-FREE shows. Say "Hey Jarvis, what time is it?" without pressing anything
- [ ] "Hey Jarvis" … wait for the chime … "open Notepad"
- [ ] "Jarvis?" gets "Yes, sir! How can I help you?"
- [ ] Keep talking without "Jarvis" for several requests; "goodbye" ends the conversation
- [ ] Talk normally for a minute without the wake phrase: nothing happens
- [ ] Restart the app: hands-free is still on

**Safety**
- [ ] "Delete the file test.txt on my Desktop" (create one first) asks first; Cancel leaves the file
- [ ] "Run format C:" is refused without asking
- [ ] "Restart the computer" asks; Cancel does nothing
- [ ] Leaving a confirmation open for 60 s cancels it

**Everyday tools**
- [ ] "Open Notepad", then "close Notepad"
- [ ] "Find PDF files in Downloads"
- [ ] "What's my battery?" / "how much RAM am I using?"
- [ ] "Set volume to 30"

**Memory and automation**
- [ ] "Remember that my editor is VS Code", then "what's my editor?"; ☰ → Memory shows it
- [ ] "Remind me in 2 minutes to stretch": the reminder appears, and is spoken
- [ ] "Start my development environment"

**Browser and screen**
- [ ] "Search the web for the latest Python release": BROWSER ACTIVE does not show (search only)
- [ ] "Open python.org and read it": BROWSER ACTIVE shows
- [ ] With screen capture on: "read my screen" shows SCREEN CAPTURE ACTIVE and reads the window behind Jarvis
- [ ] With a password manager open, "read my screen" is refused

**Logs**
- [ ] ☰ → Logs shows recent tool calls with no passwords, keys or tokens in them

If a line fails, send me what you said, what happened, and `data\logs\jarvis.log`
(the last few lines).

**Installed app (JARVIS.exe)**
- [ ] `packaging\build.ps1 -Install` finishes and starts Jarvis; the Start Menu and desktop have "Jarvis"
- [ ] The installed app has your settings, memories and chat voice (they were copied on the first install)
- [ ] `"%LOCALAPPDATA%\Programs\Jarvis\jarvis-cli.exe" --doctor` shows no `fail` lines
- [ ] Settings → Start with Windows on, sign out and in: the Jarvis window comes to the front; Ollama's window is minimised
- [ ] "Jarvis, open YouTube" and "Jarvis, read my screen" (screen capture on) work in the installed app
