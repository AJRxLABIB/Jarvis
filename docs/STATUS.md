# Status against the upgrade plan (updated 15 Oct 2026)

Verified = covered by automated tests that ran (Linux, Python 3.10, PySide6 offscreen; about 2,900 tests, 0 real
failures - the only failures seen were optional packages missing in the test environment).
Unverified = written but never run on real Windows hardware or against your real accounts.

## Built and tested
Task engine (state rules, verifying, restart-safe steps, history, one-time claims, overlap guard, parallel runner,
one-shot tool calls, global pause) - model health and fallback notice - document search with citations - to-do list 2 -
reminders (snooze, quiet hours, restart-proof) - command center with Outlook appointments - My day window -
health history and findings - daily verified backup and restore - code index (`code_impact`), `review_changes`,
lint/type checks, safer `git_commit` - knowledge-graph questions - redaction fix - self-improvement protections.

Added in the last round:
* **File-change triggers** (`schedule_task` with `when_event=file_changed`, `event_value=<folder>|*.pdf`) and
  **conditional steps** (`when`: previous_ok, previous_failed, weekday, weekend, on_battery, plugged_in).
* **Worker health and backpressure**: at most 4 automations at once (extra ones are refused and counted), stalled runs
  and a stopped worker show up in `pc_health`.
* **One ranked search** (`search_everything`): memories, documents and past conversations, each result labelled with
  where it came from.
* **Trace IDs**: every log line written during a request carries `trace_id`; `trace_request` shows the actions, task
  changes and events of one request.
* **Agents**: per-agent step and time budgets, and three new read-only agents (reviewer, verifier, analyst).
* **Experiments** (`experiment` tool, Git worktree): try risky changes in a separate folder and branch; merge only when
  your own tree is clean, conflicts are aborted, secret-looking files are never merged, nothing is pushed.
* **Integrations** (`integrations` tool, `app/core/integrations.py`): one status list for Home Assistant, the local AI,
  the phone remote, MCP servers, Outlook and the browser; new integrations register themselves.
* **Tool-call checks**: did-you-mean for a wrong tool name, oversized or non-data arguments refused.
* **Emergency stop** now also ends background commands Jarvis started (dev servers) and stops an automation that is
  already running at its next step.
* **Phone pairing review**: one-time code, 5-minute expiry, lockout, hashed keys and TLS were already right; added
  automatic expiry of a phone not seen for 90 days.
* **`python run.py --perf`**: checks that 12 written commands are understood and times the main internal steps,
  saves the result and says which steps got slower than last time.
* **Workflow checkpoints**: a workflow that stopped remembers its step; "resume <name>" (or `run_workflow` with
  `resume=true`) continues from that step and does not repeat the finished ones. A normal run starts over.
* **`security_scan`** (read-only): secrets in code, eval/exec, shell=True, unsafe loading, TLS checks off, a `.env` git
  would commit, unpinned requirements. A checklist, not an audit; findings never show the secret.
* **`python run.py --eval-quality`**: reference questions with required facts, plus "honesty" questions where the right
  answer is "I don't know"; add your own in `data\benchmark\quality.json`; results are saved per model and compared.
* **Agent output budgets**: each agent may hand back a limited amount (estimated tokens) of tool output per plan.
* Secrets: `HOME_ASSISTANT_TOKEN` and other secret settings were already encrypted for your Windows account (DPAPI) at
  every start; nothing more was needed.

## Still open
Needs your hardware or accounts (cannot be verified here):
* Real-hardware tests in `docs/TESTING.md` (microphone, hotkeys, Notepad, Edge, Ollama) have not been run.
* `JARVIS.exe` (`packaging\build.ps1`) and the Android app (`build_android.ps1`) were not rebuilt.
* Outlook appointments, Home Assistant and the phone remote were not exercised against real services.
* Windows-only dependencies were not audited for vulnerabilities.

Not built:
* Voice pipeline review (wake-word speed, streaming speech, low-confidence confirmation) - it needs a microphone and
  your voice to measure; low-confidence replies are already handled by `STT_MIN_CONFIDENCE`.
* Real per-agent *token* limits: agents are permission contracts that call tools, not separate model sessions, so the
  limit is on the estimated size of what they hand back (4 characters = 1 token).
* Running the answer-quality benchmark: needs your model running (`--eval-quality`); the harness itself is tested.
