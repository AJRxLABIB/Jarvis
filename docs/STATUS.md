# Status against the upgrade plan (updated 15 Oct 2026)

Verified = covered by an automated test that ran (Linux, Python 3.10, PySide6 offscreen: 2,800+ tests, 0 failed).
Unverified = written but never run on real Windows hardware, or needs something only your PC has.

## Built and tested
Task engine (state rules, verifying, restart-safe steps, history, one-time claims, overlap guard, parallel runner,
one-shot tool calls, global pause) - model health and fallback notice - document search with citations - to-do list 2 -
reminders (snooze, quiet hours, restart-proof) - command center with Outlook appointments - My day window -
health history and findings - daily verified backup and restore - code index (`code_impact`), `review_changes`,
lint/type checks, safer `git_commit` - knowledge-graph questions - redaction fix - self-improvement protections.

## Still open
Needs your hardware or accounts (cannot be verified here):
* Real-hardware tests in `docs/TESTING.md` (microphone, hotkeys, Notepad, Edge, Ollama) have not been run.
* `JARVIS.exe` (`packaging\build.ps1`) and the Android app (`build_android.ps1`) were not rebuilt.
* Outlook appointments, Home Assistant and the phone remote were not exercised against real services.
* Windows-only dependencies were not audited for vulnerabilities.

Not built:
* Per-worker health monitoring and queue backpressure; pause/resume checkpoints for scheduled workflows (missions have
  them); file-change triggers; conditional workflows.
* Sorting memories and documents into one ranked answer; "which tasks depend on this decision".
* Agents beyond the six existing roles, per-agent token budgets, cross-checking of agent outputs.
* Voice pipeline: not reviewed or changed (wake-word speed, streaming speech, low-confidence confirmation).
* Phone pairing review (replay protection, rate limits) and a general plug-in system for new integrations.
* Git worktree isolation for risky edits; CI-style security scanning of the project being edited.
* Moving secrets from `.env` into the Windows credential store.
* New benchmark task sets for answer quality, and timing measurements to find slow spots.
