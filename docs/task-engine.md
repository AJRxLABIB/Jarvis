# Task engine (4.0 work, increment 1)

Applies to missions (`app/core/missions.py`), scheduled automations (`app/automation/scheduler.py`) and agent
plans (`app/core/orchestrator.py`). Rules live in `app/core/taskstate.py`.

## Mission lifecycle

```
planned -> running -> verifying -> completed
              |           |
              +-> paused <+      paused -> running | cancelled | rolled_back
              +-> failed         completed | cancelled -> rolled_back
```

* `MissionStore.set_state` refuses any move the table doesn't allow (`invalid_transition`) and writes a row to
  `task_events` (schema v21): time, old state, new state, reason, run id. `MissionStore.history(id)` and the
  `mission_history` tool ("what happened to my mission") read it back.
* `verifying` runs `taskstate.verify_completion`: every step must be done or skipped on purpose. Skipped steps are
  reported in the result. A mission where nothing ran is not "completed".
* A step still marked `running` after a restart/crash/stop is not repeated blindly: read-only steps run again; any
  other step marks itself failed ("may or may not have completed") and pauses the mission. You choose: skip it,
  retry it, or cancel.

## Automations

* A task can't overlap itself: a manual run while a run is in progress is refused with a message.
* One-time tasks are claimed (disabled, status `running`) **before** their steps run (at most once). If Jarvis stops
  mid-run, the next start reports "interrupted" and does not run it again.

## Agent plans (`delegate` / `run_team`)

* A state-changing step with the same tool and arguments twice in one plan is rejected (duplicate side effect).
* Plans have a total time budget (default 600 s). Unrun steps are marked `not_run`, never "done". Cancellation marks
  the rest `not_run` and propagates.

## Model health (`app/ai/health.py`)

Role models (fast/smart/coding) that fail 3 times in a row are skipped for 30 s, doubling to at most 10 min; one
success resets. The default model is never blocked. `model_status` lists skipped models and average latencies.

## Retrieval (`app/memory/semantic.py`)

* Hybrid search: meaning score (embeddings) fused with exact-word match (70/30), so names, numbers and error codes
  are found even when the embedding model blurs them. If the embedding model is unavailable, keyword search still
  answers (logged). Re-indexing forgets documents that no longer exist, so they can't be cited.
* `ask_documents` returns numbered refs (`[1]`, file, passage number); the instruction to the model is to cite them
  and to say so when the passages don't contain the answer.

## Models and resources

A role model larger than this PC's total RAM (x1.15) is never tried (`could_ever_fit`; GPU memory isn't counted, so it
errs on the side of trying).

## To-do list 2 and the command center (schema v22)

* To-dos have priority (high/normal/low), category, repeat (daily/weekdays/weekly/monthly), "after" dependencies and a
  postpone counter. Completing a repeating item creates the next one (never in the past) and keeps the old row as
  history; completing an item says what it unblocked. `update_todo` postpones, sets the due day or priority; the third
  postpone suggests breaking the item up.
* `snooze_reminder` moves the one-time reminder that just went off (same task, no duplicate); it is stored in the
  database, so a restart keeps it.
* `command_center` ("how is my day", "what did I complete today"): overdue / due / blocked to-dos, active and waiting
  tasks, what finished today. Unreadable sources are listed, not guessed; no calendar is assumed.

## Developer tools (Phase 5)

* `code_impact` (`app/core/codeindex.py`): where a function/class/file is defined, name references, which project
  files import it (direct and indirect, Python via `ast`, JS/TS by import path) and related tests. References are
  text matches - leads to check, not proof; JS/PHP/Java/Go/C# definitions are regex-based.
* `git_commit` never stages files that look like secrets (`.env`, `*.pem`, `*.key`, `id_rsa`, `*.jks`, ...) even when
  `.gitignore` misses them: they are unstaged and reported. The confirmation lists the files and, for a push, the
  branch and remote (never forced).
* The self-improvement engine can't edit the task engine's trust boundaries: agent contracts, orchestrator, missions,
  lifecycle rules, schema migrations and the git tool (`PROTECTED` in `app/evolution/proposals.py`).

## Backup and restore (Phase 6)

`python run.py --backup-db` writes a verified snapshot to `data\backups` (keeps the newest 7); `--restore-db FILE`
checks it (integrity, not from a newer Jarvis), refuses while Jarvis is using the database, keeps the current database
as `pre-restore-<time>.db` and swaps the file in. Migrations still make their own `*.vN.bak.db` copy first.

## Second round (UI, parallelism, idempotency, health history, graph questions)

* **My day window** (Menu -> "My day", or "show my day"): Today overview, To-dos (add / complete / postpone /
  priority / delete, by item id), Mission history (state changes with reasons). Keyboard: Alt+A / C / P / D / R.
  The Background tasks window now colours each state.
* **Parallel runner** (`app/core/parallel.py`): at most N jobs at once, dependencies respected, a failed job skips its
  dependents, per-job timeout, batch deadline, cancellation stops everything. `delegate` / `run_team` accept
  `parallel` (1-4); it is used only for read-only steps of ONE agent (the agent's contract is set on the shared context).
* **One-shot tools** (`ONCE_PER_RUN_TOOLS` in `tool_manager.py`: send_email, whatsapp_message, text_contact,
  call_contact, git_commit, add_todo, write_document): an identical call that already succeeded in the same request
  returns the earlier result instead of running again. A failed call can be retried; a new request can repeat it.
* **Global pause**: with "automations off" (emergency controls) a running mission pauses before its next step.
* **Health history** (`app/core/metrics.py`, `pc_health`): one reading a minute for 14 days; findings for sustained high
  CPU, memory growth, a nearly full disk, low battery, failed automations, stuck missions, repeated failures, failing
  models - each with evidence and a next step; read-only. Settings `METRICS_HISTORY`, `AUTO_BACKUP`.
* **Daily backup** (`app/core/upkeep.py`): once a day, verified, newest 7 kept, runs in the scheduler loop's worker thread.
* **Quiet hours** now also keep reminders from being *spoken* (still shown and kept in notifications).
* **Fallback notice**: when another model answered than planned, the reply says which and that it may be less detailed.
* **Command center** includes today's Outlook appointments when Outlook answers; otherwise it says it couldn't read them.
* **Knowledge graph questions**: "what is blocking X", "what remains incomplete", "which issue caused the most failures",
  "what information is outdated or contradictory".
* **Developer**: `review_changes` (removed/skipped tests, secrets in added lines, big deletions, secret-like files),
  `check_project` accepts `lint` and `types` (only when the project configures them), `code_impact`.
* **Redaction fix**: names with a prefix (`API_TOKEN=`, `DB_PASSWORD:`) are now redacted in logs and text.
* CI runs `pip-audit` (informational). On the build machine the latest versions of the project's dependencies showed no
  known vulnerabilities; Windows-only packages (pywin32, pycaw, winrt) could not be audited there.

## Third round (triggers, health, search, experiments, integrations)

* `schedule_task` accepts `when_event=file_changed` with `event_value=C:\\folder|*.pdf`; a step can carry
  `when` (previous_ok / previous_failed / weekday / weekend / on_battery / plugged_in) and is skipped with a note otherwise.
  Only names, sizes and times of up to 5,000 files are read; the first look is a baseline, never a trigger.
* Scheduler: `MAX_CONCURRENT_RUNS = 4` (refused and counted beyond that), `health()` (loop alive, running ages, stalled
  after 30 min, refused count) feeds `pc_health`.
* `search_everything`, `trace_request`, `integrations`, `experiment` tools (see `docs/STATUS.md`); agents have
  `max_steps` / `max_seconds`; `python run.py --perf`.
* Emergency stop ends background commands and stops an in-progress automation at its next step; manual runs are not
  blocked by the automations switch (a person asked for them; the executor's rules still apply).

## Not done yet

See `docs/STATUS.md`. Tests: `tests/test_taskengine.py`, `tests/test_model_health.py`.
