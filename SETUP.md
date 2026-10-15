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
```

The first start upgrades the database to schema v23 and writes a copy first (`data\assistant.v<N>.bak.db`).

## 7. Backup and restore

```powershell
python run.py --backup-db                     # verified snapshot in data\backups (newest 7 kept)
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
- Code: "what would changing <function> affect?" (code_impact), safe edits with test checks, commits that never stage
  `.env` / keys.

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
