# Gap analysis - evidence-based (27 Oct 2026, version 3.25.0)

Method: repository, docs, git index, lint/type checks and the full automated suite were inspected in the Linux test
environment (Python 3.10, PySide6 offscreen). Nothing here was run on real Windows, a real phone, a real microphone or
your GPU. Statements are therefore "verified by automated tests" or "unverified on hardware", never "works on device".

## Baseline

| Check | Result |
|---|---|
| Size | 76,133 lines of Python in `app/`, 231 registered tools, 198 test files |
| Automated tests | all chunks pass (about 3,400 tests); skips are missing optional packages / no JDK / no OpenGL / no Playwright |
| Ruff | clean after this audit (6 findings, all in the new command-center code, fixed) |
| Mypy | 990 findings at HEAD before this round's work, about 998 now - mostly Qt stubs and dynamic attributes; not a gate. The new code adds a few (Qt `activated=` stub mismatch, optional `Capability`) |
| Versions | `app/__init__.py`, `pyproject.toml`, `SETUP.md`, installer script, `version_info.txt` all say 3.25.0. README header said "version 1.0" (fixed) |
| Python | `requires-python >=3.11`; tests here run on 3.10 (works because the code avoids 3.11-only syntax in tested paths; **Windows 3.11+ run is the supported target and was not run here**) |

## Confirmed defects found and fixed in this pass

1. **Secrets tracked in git.** `android/signing/jarvis-wake.jks`, `android/signing/password.txt`, `.env.bak` and a
   `Jarvis-updated.zip` were in the git index (first commit onward). They are now untracked and ignored; the files are
   still on disk. They **remain in git history**. If `origin` (GitHub) is or was public, treat the signing key as exposed.
   Replacing it means a new key, and phones with the old signature must uninstall before installing - your decision.
   `.env.bak` held no token values.
2. README status line was stale ("version 1.0, all 14 phases done").
3. Lint errors in the command center.

## Status by area (what exists / what is only unit-tested / what is open)

| Area | Implemented and unit/integration-tested | Not verified on real hardware | Open gaps |
|---|---|---|---|
| Orchestration, planning, schema-validated tool calls, taint firewall, risk levels, confirmations, emergency stop | yes (executor pipeline, missions, verification) | - | evaluation set is small (`test_quality_eval.py`, `app/benchmark.py`); no recorded latency percentiles on your hardware |
| Model routing | roles, fallback notice, memory-aware bigger-model swap | GPU memory figures on a 6 GB card | measured-quality routing is limited to the existing benchmark |
| Windows control | app/window/file/screen tools with before/after verification | **all of it** (UIA, OCR, scaling, multi-monitor, Excel COM) | no real-Windows end-to-end run recorded |
| Android | signed protocol, pairing, queue, 1.1-1.4 actions, lock admin | **all of it**; the pairing fix (1.4.0) needs a rebuilt APK | foreground-service / battery cost never measured |
| Security | audit trail, standing approvals, FULL_POWER limits, Security view | - | history rewrite for leaked files is a user decision |
| Memory / knowledge graph | SQLite memory, provenance, graph questions | - | graph browser added (command center > Knowledge): filter by kind, search, node details with every connection and its evidence, ask box; built in a background thread; unit-tested offscreen, not seen on your screen |
| Task engine | persistent steps, retries, checkpoints, history | restart after real power loss | - |
| Productivity | reminders, to-do, habits, expenses, calendar (Outlook) | Outlook COM, real accounts | local calendar conflict view is thin |
| Voice | wake word, STT/TTS, push-to-talk | **real mic/speaker latency and echo** | no measured latency numbers |
| UI | HUD + command center | real display, DPI, GPU, screen reader | - |

## Integration classes

Fully local: memory, tasks, scheduler, files, documents (full text), local models (Ollama), Piper/Kokoro voices,
Whisper STT, phone link on your Wi-Fi, encrypted backup. Ordinary internet: web search/news/weather, Edge neural
voices. Optional account: Google Calendar/Drive, GitHub, Outlook mail sync. Key / paid: none required (Firebase push is
optional and off). Separate application: Ollama, Microsoft Edge/Excel/Outlook, JDK + Android SDK for the APK.
Not implemented: measured battery profile.

## Next actions that need you

- Decide about the signing key and git history (above).
- Run the hardware checks listed in `docs/STATUS.md` ("Unverified") on your PC and phone; send results or failures.
