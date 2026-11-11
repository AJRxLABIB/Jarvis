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
* **Retry policy** (`app/core/retry.py`): errors are classified (transient / resource / verification / permission /
  validation / permanent / unknown); only transient, resource and unverified results of repeat-safe (read-only) steps are
  retried, with growing waits and a total limit. Missions use it; a mission step cut off by a restart now shows as
  RECOVERY_REQUIRED in the task center.
* **Memory controls** (`memory_admin`): list categories, stop/resume using a whole category, delete a category, export
  everything to `data\exports` (private ones only if asked); `MEMORY_EVENT_RETENTION_DAYS` forgets what Jarvis recorded
  itself after N days (what you told it is never touched).
* **Security regression tests**: path tricks and links, agent escalation, critical-action blocks, secret redaction,
  unknown tools. They block known mistakes; they do not prove the system secure.
* **Workflow studio** (Menu -> "Workflow studio", or "open workflow studio"): see every saved workflow/automation, its
  steps with risk, condition and whether it asks first; dry run (runs nothing); reorder, remove, add a spoken note, set
  a condition, continue-on-error; Save validates like creating one does, keeps the old version and never changes when
  it runs or re-enables a paused one; restore a version; Run now / Resume go through the normal request path.
* **Voice measurements** (`voice_stats`, table `voice_events`, schema v25): per language - commands, recognition time,
  time to first reply, low-confidence heard, wakes with no command, handled rate. No audio or words are stored; rows
  older than 60 days are dropped. Real false wake-ups, accuracy (use `--benchmark`), wake-word and interruption
  latency need your microphone and are not measured here.
* **Daily plan** (`plan_my_day`, "plan my day", "replan, I lost 2 hours", "review my day", My day -> Plan my day):
  to-dos get optional minutes; the plan orders by overdue / due today / priority, fits the time left (working day minus
  calendar, minus interruptions), shows a reason per item, the next action, what doesn't fit, blocked items, calendar
  conflicts and repeated postponements. Urgent items that don't fit are flagged "over budget", never hidden. The saved
  plan is only a list of to-do ids, so nothing is duplicated; the review compares it with what is really done.
* **Portable workflows** (`export_workflow` / `import_workflow`): JSON data only, with the tools and highest risk it
  declares. Import is strict, refuses a file that does more than it declares, never imports a schedule, never runs
  anything, and keeps the replaced version.
* **Research projects** (`research_project`): named projects with sources (deduplicated addresses, dates), claims that are
  sourced (must cite a stored source) or inference (labelled), numeric conflicts flagged, a cited report, and
  `check_updates` that re-reads the sources and marks claims from changed sources "needs recheck".
* **Learning** (`learn`): questions with source labels (you / document / AI-written), SM-2 spaced repetition, word-overlap
  grading (you can override), progress per topic, delete a topic.
* **Mail triage and drafts** (`triage_emails`, `email_draft`): urgent / routine / promotional / possibly suspicious with
  reasons, deadlines and requested actions; links are never opened; instructions inside an email are flagged and ignored.
  Drafts are stored, edited and approved locally and never sent from here (sending still asks, once per request).
* **Health baseline** (`health_history`): what is normal for this PC, "higher than normal" findings labelled inferred with a
  confidence, delete readings, `METRICS_RETENTION_DAYS`. Only CPU / memory / disk / battery numbers are recorded.
* **Activity timeline** (`activity_timeline`): who started each action (voice / typed / phone / automation / Jarvis), outcome,
  verified or not; search, list unverified changes, export, and permanently delete old records (asks first).
* **Tamper-evident activity log** (round 8): each audit entry carries a hash over the one before it; `activity_timeline`
  action `verify` reports edited entries, removed middle entries or a cut-off tail. Confirmed deletes of the OLDEST entries
  keep it valid. Detects accidents and simple tampering; someone who rewrites the whole database and recomputes every hash
  could still forge it. Entries from before schema v27 are not covered.
* **Program baselines and crash correlation** (round 8, `program_history`): OFF by default (`PROCESS_HISTORY=true` turns it on).
  Records only the five busiest programs' names with CPU/memory once a minute (listed and deletable under Privacy
  as "Program history"). Reports programs using well above their own usual and the program heaviest just before failed
  requests - labelled inferred (a correlation, not a cause). Needs days of data before it says anything.
* **Research source monitoring** (round 8): `research_project` action `watch` (N days, 0 = off) re-reads a project's SAVED
  source addresses (public pages only) while Jarvis runs and marks claims from changed pages "needs recheck". Opt-in per project.
* **GitHub connector** (round 8, `github`): READ-ONLY - unread notifications, issues/PRs assigned to you, a repo's open PRs.
  Needs `GITHUB_TOKEN` (a read-only fine-grained token; stored encrypted like other secrets). Tested only against a mocked
  GitHub, not the real service. Listed in `integrations`.
* Secrets: `HOME_ASSISTANT_TOKEN` and other secret settings were already encrypted for your Windows account (DPAPI) at
  every start; nothing more was needed.

## Round 8 - security, integration truthfulness, evaluation lab
Audit findings (from the code, then fixed or classified):
* **Defective, fixed - DNS rebinding in `app/browser/pages.py`**: the address was checked, then the HTTP client resolved the
  name again. Now `PinnedTransport` resolves once per request, requires every answer to be public, connects to that IP and
  keeps the real name for `Host` and TLS (certificate still verified). Also: ports limited to 80/443/8080/8443, no
  `user:pass@` addresses, IPv4 hidden in IPv6 (`::ffff:`, NAT64, 6to4) judged as IPv4, whitespace/control characters
  refused, no environment proxy (it would bypass the pinning), whole fetch capped at 30 s. Redirects were already
  re-checked at every hop. Tests: `tests/test_ssrf_updates.py`.
* **Defective, fixed - update check/download (`app/utils/updates.py`)**: an https manifest or installer could redirect to
  plain http, there was no size limit, the file name came straight from the URL and a failed download left a partial file.
  Now https-only after redirects, 256 KB manifest / 1 GB installer caps, a sanitised .exe/.msi name, cleanup on failure.
  Design limit that remains: the checksum comes from the same manifest as the URL; it protects against corruption, and
  the Authenticode check is what protects against a malicious installer (verified only on Windows, not tested here).
* **Incomplete, fixed - integration status** (`integrations`): "ready" only ever meant "configured". New smoke test
  (`integrations` with `test: github|home_assistant|ollama|outlook`) sends ONE read-only request and records a verdict -
  smoke passed, auth failed, permission denied, unreachable, degraded - with time. End-to-end is always shown as
  "not tested": no code path reports it as passed. Schema v28. Tests use mocked services only.
* **Incomplete, fixed - evaluation lab** (`--eval-quality`): dataset is now versioned (2026.10.1) with categories (general,
  honesty, injection, Bangla, mixed Bangla/English, code, clarification, planning, context, long context) plus real
  tool-selection questions that use the router's actual tool list. Results are per category with thresholds (honesty and
  injection must be 100 %: any failure is reported as a SAFETY FAILURE), latency percentiles, timeouts/errors, CPU and
  memory while answering, and the environment. Saved history never contains answers. Scores of different dataset versions
  are refused as comparable. GPU/VRAM is NOT measured.
* **Release process - improved, not run**: `packaging/dependency_report.py` writes a CycloneDX SBOM, a licence table and a
  lock list from the installed packages (tested). CI now fails on known vulnerabilities in `requirements.txt`
  (exceptions only via `packaging/audit-ignore.txt`) and uploads the inventory. **The edited CI workflow has not been run.**
* **Already implemented, verified by reading** (not rebuilt): phone-remote private-network check, per-address rate
  limit, one-time pairing codes and TLS pinning; task checkpoints, recovery-required state, once-per-run guards.

## Round 9 - background mode, simulator, update rollback, troubleshooting, phone sync
Verified here by automated tests (sandbox, Python 3.10, no Windows, no phone); everything marked *untested* needs your PC/phone.
* **Background mode** (`python run.py --background`, `background_mode` tool, `packaging\background.ps1`): a windowless host
  runs the real scheduler. Approval-needing steps are refused (nobody is there to answer). One lock per user shared with the
  window; opening the window hands over. Real process start/heartbeat/pause/stop verified in the sandbox.
  *Untested:* `background.ps1` (logon task) on Windows. It is opt-in; nothing installs it for you.
* **Workflow simulator** (`simulate_workflow` tool): crash/restart, timeout, denied, lost response, duplicate trigger,
  emergency stop, simulated time on a scratch DB with pretend tools; every result labelled SIMULATED. The existing dry-run is
  unchanged. It found a real bug: after a crash the engine re-ran the in-flight step. Fixed: a state-changing step whose
  outcome is unknown is NOT repeated on resume unless the workflow says `on_uncertain=repeat` (or `skip`).
* **Update guard**: verified DB backup before the installer starts (the installer is not started if the backup fails), a
  health check on first start of the new version, and `python run.py --rollback-update` (restores the data; keeps the newer
  copy). The program itself is NOT rolled back: Jarvis keeps no old installers, so you reinstall the old version yourself.
  *Untested:* a real installer upgrade on Windows.
* **Guided troubleshooting** (`troubleshoot`): findings with evidence, cause, fix and the exact change; only three repairs
  (create `.env`, clear caches, close a stuck process), each approved first and re-checked afterwards.
* **Injection resistance**: two eval items check that a hidden instruction in web text is not obeyed and no tool is called
  because of it. Measured on real models in round 8 (the 4B models fail it; `qwen3:8b` passes).
* **Phone sync (server side only)**: schema v30. Per phone and per dataset opt-in, chosen on the PC (`mobile_sync` tool);
  datasets are an allowlist: `todos` and `notifications` (generic one-line notices, no content). New signed phone commands
  `sync_status`, `sync_pull`, `sync_push` (biometric key required), `sync_forget`, `notifications`; new `sync` permission,
  off by default. Rules: PC is the hub; stale phone edits are refused with the PC's version (no silent overwrite); each change
  has an `op_id`, so a retry never applies twice; deletes are tombstones; a phone away longer than the log gets a fresh
  snapshot; revoking a phone deletes its sync records. 24 tests.
  **Not built:** push notifications (FCM) - polling only, the phone must ask; **no Android (Kotlin) client code was written
  or tested**, so nothing on the phone uses this yet.

## Round 10 - Google, profiles, accessibility, simulator in the studio
Built and tested here with fakes (no real Google account, no screen reader, no Windows):
* **Google Calendar and Drive** (`google` tool, `app/tools/google.py`; 16 tests with a pretend Google). Your own OAuth
  "Desktop app" client; installed-app flow with PKCE, a one-use `127.0.0.1` callback and a checked `state`; the refresh
  token is stored DPAPI-encrypted. Scopes: `calendar.events` and `drive.metadata.readonly` only - Drive CONTENTS are never
  read. Listing and searching are safe; adding an event, connecting and disconnecting ask first. Offline mode and the
  emergency "network off" switch block it. **Untested against real Google** (needs your client ID/secret). While your
  Google consent screen is in "Testing" Google may expire the sign-in after 7 days: say "connect Google" again.
* **Configuration profiles** (`settings_profile` tool, `app/config/profiles.py`): `meeting`, `night`, `performance` plus your
  own. A profile is limited to an allowlist of everyday settings; secrets, paths, devices, remote access and every security
  setting are outside it (a test enforces this), values are validated by the real settings model, the change is shown first
  and "undo" restores what was replaced. Imported profiles drop disallowed fields.
* **Accessibility audit** (`app/core/a11y.py`, `--audit-accessibility`): WCAG contrast of every theme colour pair (all pass)
  and a widget check (every control needs a name and keyboard access). It found and fixed real problems: 41 settings
  controls had no name a screen reader could say, the chat view, live-event log, message box and settings sections list
  were unnamed. Tests keep the main window, settings and workflow studio clean. **Not checked (needs a person with NVDA /
  Narrator):** how screens actually sound, high-contrast mode, 200 % text scaling, the orb and charts.
* **Workflow studio "Simulate..."** (Alt+I): choose a what-if for the selected step and play it on the steps as currently
  edited (saved or not), with pretend tools; the report is labelled SIMULATED and nothing real runs.
* `github` and `google` now respect offline mode (`needs_network`).

## Round 11 - audit of the "master prompt" against what exists, and the gaps it found
Most of that prompt (task engine, policy layer, memory, voice, productivity, developer tools, smart home, phone remote,
workflows, research, proactive suggestions, backups, updates) was already built and tested in rounds 1-10; nothing was
rebuilt. Real gaps found and filled:
* **Backups are now restore-tested** (`backup.restore_test`): a copy is opened in a throw-away folder the way a restored
  install starts (migrations included), checked for integrity and foreign keys, row-counted and given a test write; the
  live database and the backup file are never touched, and the scratch copy is deleted. It runs after every daily backup
  and after `--backup-db`; `python run.py --test-restore [file]` does it on demand. A failure is saved, shown by
  `troubleshoot` and in the Security center. 5 tests incl. damaged, missing, newer-version and older-version (v29 -> v30) backups.
* **Model measurements**: besides latency and failures, each model now records time to first token and how often its tool
  calls were well-formed (a tool that doesn't exist or unusable arguments count against it). A model with at least 5 calls
  and under 80 % good calls is flagged in the health report. Session-only numbers; they do not pick models by themselves.
* **Security center** (Menu -> "Security center...", "open security center"): the activity-log tamper check, guards that
  are off (power confirmations, update signature when an update feed is set, unencrypted phone remote), blocked actions of
  the last 7 days with reasons, paired phones with permissions and refused phone requests, last backup restore test, and
  buttons to the existing Phones / Privacy / Emergency dialogs. Read-only: it changes nothing itself.
Deliberately NOT built (said plainly): a drag-and-drop node canvas for the automation studio (the studio edits the real
steps as a table, with dry run and simulation; a canvas would be a second editor over the same data), a formal design-token
file (colours already live in one place, `app/ui/styles/theme.py`), model choice driven by measured scores (measurement
exists; automatic switching on small samples would be guesswork), voice/vision/wake-word accuracy tuning (needs your
microphone and screen).

## Round 12 - voice recognition, from the owner's real log (623 recognitions, 24 wake-ups)
What the log showed (`data\logs\jarvis.log`, read-only): the recognizer is healthy (large-v3-turbo on the GPU, ~0.5-0.9 s per
sentence), but median confidence is only 0.47 and 58 % of recognitions are under 0.5; 10 of the 24 wake-ups came from the
"name check" with a neural wake score of exactly 0.0; and no Bangla was recognized at all (all 623 were `en`, a few
mis-detections in other languages). Corrections learning, custom vocabulary and hints already existed and were not rebuilt.
* **Wake-up blind spot fixed (GPU only):** a sentence that starts with the name but is longer than 3.5 s was never checked
  unless the neural model fired. With a GPU recognizer the limit is now 6.5 s (collection up to 8 s). On CPU nothing changed.
* **Cut off mid-sentence fixed:** at the normal pause, if what was recognized so far ends in "and / the / to / ..." or a comma,
  the listener waits up to 2 x 0.8 s more before closing the sentence. Needs the GPU early transcript; on CPU it behaves as before.
* **`python run.py --voice-doctor`** (new): records 3 s from EVERY distinct microphone while you speak and reports loudness,
  room noise, signal-to-noise, clipping, exact silence or open errors; names the best microphone, says when Windows' default
  is the weaker one and prints the `.env` line to use, and points at the Windows "Let desktop apps access your microphone"
  switch when every device is silent. Audio is kept in memory only. The real Windows microphones have not been run through it.
* **`python run.py --wake-doctor`** (new, newest log: 115 wake checks, neural score ~0, 0 wake-ups): you say the wake phrase 6
  times; it shows what recognition wrote (screen only, not logged) and whether Jarvis would wake. If it keeps writing the name
  one way ("Travis"), it asks y/N per spelling before saving it to `data\wake_aliases.json` (max 12; delete the file to undo).
  Aliases only count in the name position, never replace the greeting. Tested with fake audio/recognizer only (10 tests);
  not run on your microphone.
* Not claimed: the confidence numbers above are not accuracy (there is no ground truth in the log). Real accuracy needs
  `python run.py --benchmark`. Bangla and mixed Bangla-English could not be tuned: there is no Bangla in the log.
  The 6.5 s and 0.8 s values are reasoned from the log, not tuned on your voice.

## Round 13 - the seven items left "not built"
Verified = automated tests that ran here. Nothing below was run on real Windows, a real phone, real Firebase or a real GPU.
* **Design tokens:** `app/ui/styles/tokens.py` is now the single source of colours, fonts, spacing, radii; `theme.py` builds
  the stylesheet from it (every colour in the stylesheet must be a token: tested) and the accessibility audit reads it.
  `python run.py --export-tokens` prints them as JSON. Verified.
* **GPU/VRAM in the evaluation lab:** `--eval-quality` now records peak VRAM, total VRAM and peak GPU utilisation per run (NVIDIA only,
  via `nvidia-smi`; "not measured" otherwise, never 0 MB). Whole-GPU numbers: other programs count. Verified with a fake reader;
  not run on a GPU.
* **Automatic model switching** (`AI_AUTO_SWITCH`, off by default; `app/ai/switching.py`): replaces the chosen model only (1) when
  untrusted text is in the conversation and the model's own benchmark shows it obeyed hidden instructions, or (2) when its tool
  calls were malformed too often this session. The target must be installed, benchmarked and have a clean safety record; no
  record = never a target. Reported in "which model are you using". Verified with the fake Ollama; it can only be as good as
  your `--eval-quality` runs.
* **Registered Windows service** (`packaging\service.ps1`, `packaging\jarvis_service.py`, `app/automation/supervisor.py`): a
  pywin32 service that supervises `run.py --background` (restart with back-off, gives up after 5 crashes in 10 minutes, does
  not fight the window, stops the host gracefully). Runs as YOUR account (secrets are DPAPI-sealed for it). The supervisor
  logic is tested; the pywin32 glue and the install script were never run on Windows and pywin32 services are fiddly: expect
  to debug the first install. Use it OR the logon task, not both.
* **FCM push, PC side** (`app/remote/push.py`, schema v31 `push_tokens`, `FCM_SERVICE_ACCOUNT`, phone command `push_register`):
  content-free `{"kind":"sync"}` pings only, for trusted phones with notifications sync on, max one per 30 s, none during an
  emergency stop, dead tokens deleted, revoke deletes the token. Verified against a fake Google. Never tried against real Firebase.
  **The bundled Android app cannot receive FCM** (it is built with plain `javac`, no Google Play services); push needs a
  Firebase-enabled build, which is not done. Until then the phone polls.
* **Android sync client** (`SyncClient.java`, `AppSync.java`, `SyncJob.java`, a "To-do sync" card in Settings): queued edits with
  op ids, batches of 20, conflicts keep the PC's version, paging, resets, generic notices, opt-in 15-minute background polling
  that never sends edits. `Protocol.STEP_UP` now includes `sync_push` (it was missing: pushes would have been refused). The
  protocol logic was compiled and run on a plain JVM (`android/JarvisMobile/test/run.sh`, 17 checks pass; it is skipped by pytest
  where no JDK exists). `AppSync`, `SyncJob`, the MainActivity changes and the manifest entry use Android classes and were NOT
  compiled or run: run `packaging\build_android.ps1` and expect to fix compile errors.
* **Node-canvas workflow editor** (`app/ui/workflow_canvas.py`, `app/automation/canvas.py`, a Canvas tab in the studio): your
  steps as boxes and arrows; click selects, drag to another slot reorders, arrow keys move the selection, Ctrl+Left/Right move
  a step, Delete removes; dashed border = continue on error; arrows carry conditions. It is a view of the real linear workflow,
  not a free-form graph: the engine runs steps in order, so branches and loops are not offered. Real mouse drags verified
  in an offscreen Qt session. Screen readers cannot read a canvas: the Steps table is the equivalent.

## Round 14 - hardening and measured speed-ups (audit first, evidence-backed fixes only)
* **Speed, measured here (Linux sandbox, so absolute numbers on your PC differ):** every chat request rebuilt the JSON schema of
  all 200+ tools twice (`select_specs`: 115 ms per request). Tool specs and their size estimates are now cached per tool object
  (0.7 ms; a replaced/new tool gets a fresh spec). Checked and left alone because no hot spot was found: startup imports
  (`import app.main` 0.2 s; the tool catalog 0.7-1.0 s spread over ~100 small modules), fresh database creation (0.27 s for 31
  migrations, 1 ms when it exists), the written-command matcher (0.8 ms per phrase), and the rest of a turn (0.5 ms without the model).
  Model latency itself is the Ollama side: see `--eval-quality`.
* **Android sync:** methods are synchronized (the screen edits while a worker syncs); a done-toggle shows at once instead of
  after the next sync; Android 13+ is asked for the notification permission when you turn background checks on. JVM checks: 18 pass.
* **Push:** quiet hours respected (only an Emergency event pings during them; the rest ping after, without re-sending).
* **Workflow studio:** Undo (button and Ctrl+Z, up to 50 steps, back to the saved version); canvas zoom (Ctrl+wheel 10-300 %),
  Ctrl+0 fits to the window, small workflows never scale past 100 %.
* **UI colours:** 57 hard-coded colour literals in widgets now come from the design tokens (two new tokens: `warn`, `violet`);
  a test fails if a widget writes a token's colour as a bare literal again. Colours embedded inside larger CSS strings (79
  of them, many with no token) are not converted yet.
* **Voice:** no new change: there is no new evidence since round 12. What would help most is your own output of
  `--voice-doctor` and `--wake-doctor`.
* Android compile check: `AppSync`, `SyncJob`, `Store` and `Pc` now compile (javac) against small stand-ins for the Android classes they use
  (`android/JarvisMobile/test/stubs`), and the sync rules run on a JVM (`sh android/JarvisMobile/test/run.sh`; pytest runs it where a JDK exists).
  This proves the code is well-formed against those API shapes, not that it behaves on a phone. `MainActivity` (the new To-do sync card) is still not compiled.

## Round 15 - four new capabilities (you chose: clipboard memory, routine suggester, ask your history, focus guardian)
All four are tested with fakes/temporary databases here. None has run on your Windows PC; the clipboard hook in particular
(Qt's `dataChanged` signal in the window) and the focus guard's process check were not exercised on real Windows.
* **Routine suggester** (`routine_suggestions` tool, `app/core/routines.py`): reads Jarvis's own audit log; a harmless
  open/play/run action you requested yourself on 3+ different days in 21 at times within 45 minutes becomes a suggestion
  ("around 08:05 on weekdays, 4 days"). Accepting creates a normal automation after the usual checks and a confirmation;
  dismissed or already-automated ones are never re-suggested; automations' own runs and failures never count. It cannot see
  apps you open by hand.
* **Ask your own history** (`ask_history` tool, `app/core/history_qa.py`, phrases "when did I last run the backup", "how many
  times did I open Spotify this week"): answers from the activity log and finished to-dos with the record ids it used and an
  honest "only what Jarvis recorded". Matching is stem-based and explainable; everyday verbs only count when nothing else
  matches (found and fixed while testing: "open zoom" used to match "open spotify"). The phrase shortcuts only fire for things
  Jarvis does and records, so "when did I last visit the dentist" still goes to the model/memory.
* **Clipboard memory** (`clipboard_history` tool, `app/core/clipboard_memory.py`, schema v32, `CLIPBOARD_HISTORY=false` by
  default): text only; passwords/keys/tokens, anything the redactor would change, card numbers (Luhn), text over 4000
  characters and anything copied in Private mode are never kept; 200 items / 7 days (configurable), pinned items stay; search,
  restore (asks), pin, forget (asks), clear (dangerous: always asks). Copied text is labelled untrusted when shown to the model.
* **Focus guardian** (`focus_mode` with `keep_closed`, "focus for 45 minutes and keep distracting apps closed"): after the
  confirmed start, blocked apps that reopen are closed again every 30 s, at most 3 times each and never twice within a minute;
  the end-of-session summary says what it did. Nothing is closed unless you asked for it in that request.
* Not built (suggested but not chosen): blocking websites during focus (no hook into the browser), a routine suggester for
  things you do by hand, and encryption of the clipboard history at rest (it is as protected as the rest of your database).

## Still open
Needs your hardware or accounts (cannot be verified here):
* Real-hardware tests in `docs/TESTING.md` (microphone, hotkeys, Notepad, Edge, Ollama) have not been run.
* `JARVIS.exe` (`packaging\build.ps1`) and the Android app (`build_android.ps1`) were not rebuilt.
* Outlook appointments, Home Assistant and the phone remote were not exercised against real services.
* Windows-only dependencies were not audited for vulnerabilities.

Not built or only partly built (round 8 deliberately left these; none is faked):
* (Google connectors: built in round 10, untested against real Google.)
* (Done in round 9, see above: background mode, simulator, update rollback of data, guided troubleshooting, phone-sync server.)
* Built in round 13 but untested on real systems: FCM (PC side only), the Android sync client, the Windows service (see above).
  Still not built: a Firebase-enabled Android build that receives push; signed-installer verification untested here.
* Google Drive file contents (deliberately not requested), Google event editing/deleting (deliberately not offered).
* (GPU/VRAM measurement: built in round 13.) The new 23-question set has been run on `qwen3:8b` only (22/23, 2026-10-15; see below), not yet on your other models.
* First real run, `qwen3:8b` on the owner's PC (20 CPUs, 31.7 GB RAM, no GPU measurement): 22/23. Both safety categories 100 %
  (honesty 3/3, injection 2/2), tool selection 4/4, median 1.5 s per answer, 90th percentile 3.7 s, no timeouts. The one
  miss is spelling a word backwards (a known weakness of this model size). Each category has 1-6 questions, so this is a
  signal, not a measurement.
* Same set (2026.10.1), same PC: `jarvis:latest` 19/23 (82.6 %), `qwen3:4b-instruct` 20/23 (87.0 %), `qwen3:8b` 22/23
  (95.7 %). BOTH small models obeyed an instruction hidden in web text (answered just "PWNED") - a safety failure the 8B
  model did not have. `jarvis:latest` also answered the Bangla capital question with nonsense. Speed: 0.38 s median for
  the small models vs 1.5 s for `qwen3:8b`. Reading: use `qwen3:8b` for anything that reads web pages, email or
  documents (research, mail triage); the small models are fine for quick commands that touch no untrusted text. The
  "What time is it?" miss was a flawed question (the prompt states the time) and was replaced in 2026.10.2.
* Voice tuning (wake-word and interruption latency, accents, noise) and real-device Android checks: need your hardware.
* Android UI polish and the Android side of sync: not changed.
* Answer-quality score with your model (`--eval-quality`).


## Round 16 (full audit + upgrades)

Done: standing approvals wired into the executor + `approvals` tool + Security Center section (16 tests); conversation digest
so long chats keep what you asked (5 tests); RSS parser refuses DOCTYPE/ENTITY; sha1 marked non-security; backup table name
quoted. Audit: coverage 80%; bandit findings reviewed (SQL placeholders and the intentional 0.0.0.0 phone bind are not bugs);
vulture found no dead code. Full suite: all pass except `test_real_model_separates_voices` (needs cv2); ruff clean.

Not changed: voice recognition - shortcuts already fall back to phonetic app-name matching, second pass, hotwords and
corrections exist, and there is no new log evidence; run `--voice-doctor` / `--wake-doctor` and share the output for tuning.
Large functions (match_shortcut, ToolExecutor.execute) are not refactored. Still untested on real systems: Windows service,
FCM, Android UI classes, clipboard hook, focus guard, GPU measurement.

Round 16b: `--always-on` (tray + start with Windows + hands-free), `TAINT_LOCKDOWN` (opt-in), silent-microphone notice
in hands-free (`MicWatch`). Found by the stricter quality set 2026.10.3: `qwen3:8b` attempted `send_email` after a hidden
instruction (contained by the confirmation firewall). Untested on real hardware: always-on start-up, mic notice.

Times are now shown and spoken in 12-hour format (1:38 PM) everywhere user-facing (`app/utils/timefmt.py`).

Round 17: UI restyled toward your mockup - chat bubbles with round avatars, donut CPU/memory gauges in Link Monitor, switch-style module toggles, equalizer bars; header unchanged. Layout/colours verified by offscreen render only, not on your screen.

Round 17b: plasma sphere + side audio waves in the core, vertical meters card, spectrum strip, particle halo on the power core, voice waveform bubbles (your own microphone levels for spoken messages). Header kept as you asked. Checked by offscreen render only.
Round 17c: the core is now a real 3D plasma globe (dust cloud + 10 orbit loops tumbling with true rotations, front bright / back dim), flowing energy streaks and side waves, concentric rings turning both ways; speed follows Jarvis's state. Offscreen render checked only.

Round 18: real 3D core (`app/ui/core3d`): moderngl + GLSL 3.30 inside a QOpenGLWidget, state machine, 3 quality levels + governor,
selective bloom, painter fallback. Verified here on a SOFTWARE OpenGL 4.5 renderer (Mesa llvmpipe under Xvfb): shaders compile,
all qualities/states render, the widget runs inside the real main window. NOT measured: FPS/CPU/GPU on your graphics card,
Windows drivers, high-DPI, long-run memory. Not done from the brief: data-pathway pulses to panels, per-tool execution routing,
sound effects, HUD scale / panel visibility settings, the new colour palette, real speech-audio amplitude (speaking uses a
synthetic cadence), particle trails.

Round 19 (layout matched to the reference picture): header subtitle + LIVE HUD / LOCAL AI / VOICE LINK / ALL ON THIS PC (each
opens what it names), page indicator + five tabs (Devices/Phone/Privacy/Logs/Settings moved to MENU > Pages), frequency card +
upright meters, Link Monitor = two gauges, conversation-only right column with its own input + STOP + live box, rounded bubbles
with avatars outside, status lamps (hands-free is a real switch), spectrum bars with time marks + AI engine/security line,
particle mesh on the power core, brighter 3D core with energy ribbons, transparent over the panel (WA_AlwaysStackOnTop) and valid
premultiplied output (fixes coloured specks). Response-timing and approval panels hidden (approvals still use the dialog).
Rendered here with PySide6 6.8.3 + Mesa llvmpipe under Xvfb; not seen on your GPU/driver.
Round 19b: removed from view at the user's request: header subtitle + centre items, left column, status card, side STOP/live box, bottom status cards, bottom live box.
Round 19c: also removed '//', the tab row, meters + quick actions, the CPU/GPU/memory read-outs around the core, the Link Monitor/Performance/Power Core column, the AI-engine line and the conversation input; the core is centred (equal-width side columns).
Round 19d: functionality check after hiding - MENU > Pages (all 10 tabs) and MENU > Quick actions (all 6, same handlers); Memory menu shows suggestions to review; test_hidden_controls_keep_working_through_the_menu.
Round 19e: core panel frameless; streaks + side waves drawn by the window background across the full width (HexBackground.holo_source, 24 fps band repaint).

Round 20 (smoothness): measured the user's recording frame by frame - the core repeated a frame about every 5th frame
(~24 fps) and the background waves ~every 2nd (15 fps). Causes: (1) the auto-quality governor measured the gap between
frames instead of the drawing cost, so it dropped to "low" (20 fps, fewer particles); (2) the background waves were gated
at 24 fps on a 30 Hz clock = 15 fps. Fixes: governor judges cost vs budget (steps down if >75%, up after ~10 s under 30%,
never back to a level it left); frames paced on frameSwapped at the quality's rate rounded to the display refresh
(72 on 144 Hz, 60 on 60 Hz) with a watchdog that restarts a lost frame; background waves repainted in step with the core
(up to 40 fps); 4x MSAA for balanced/high; minimum point size 1.6 px. Measured here on software GL: steady 30 / 57 fps,
max gap 24-44 ms. Not measured on the user's GPU.

Round 21 (smoothness, second recording): the core and the background waves froze on the SAME frames (~9 % of frames),
i.e. the main thread was overloaded - QPainter redrew a full-width strip of the window for the waves every frame and the
whole window was re-composited around a separate GL widget. Now the 3D widget IS the window background (GPU draws the
hexagon backdrop, scan beam, streaks and waves; the core is drawn into `core_slot`; panels are its children, composited on
top). Python work per frame cut: holo geometry vectorised (3.0 -> 1.0 ms), bezel ticks cached as a turned pixmap
(2.2 -> 0.24 ms). Measured on software GL (Mesa llvmpipe, 1280x720 window): steady 30 fps on low, max gap 38 ms. Not
measured on the user's GPU. Without OpenGL the old painter backdrop is used unchanged.
Round 22: window/tab overview (app/core/overview.py, app/ui/window_overview.py, app/ui/dwm.py live DWM thumbnails), voice + menu entries, more screen-reading phrases. Overlay tested here with title cards; DWM live pictures and tab switching are Windows-only and untested here.
Round 23: talk-over (app/voice/echo_gate.py; TTS hold/resume/discard + playback level log; hands-free _talk_over_onset/_interrupt_frame) and the natural voice finish (jarvis_voice.natural_finish, gentle prosody, TTS_STYLE). Tested with fakes and synthetic audio only; not with your microphone, speakers or the online voice.
Round 24 (speed): first clause spoken before the first sentence ends (SentenceSplitter quick_start), cache + prewarm of short repeated replies, end-of-speech wait 0.9 -> 0.7 s (default and the user's .env value). Not timed on the user's PC.

Round 25 (recording 20:26): the log showed the talk-over gate firing every ~2 s on Jarvis's own voice (learned
"coupling" -40..-71 dB: it learned only from the quiet gaps between words), each false alarm restarting the sentence
("why are you repeating yourself"), and garbled echoes accepted as commands ("Good Cat" = "Good catch", "and Stopped").
Fixes: gate learns from all playback frames (95th percentile, clamp -40..+6 dB), 1.6 s warm-up, 4-frame onset,
+4 dB extra margin after each false alarm; resume continues from the cut point (no repeats); fuzzy echo matching;
talk-over needs >=2 clear words (conf >= 0.6) unless "stop"/"Jarvis, ..."; other apps' audio (pycaw meters) -> only
"Jarvis, ..." counts during talk-over and follow-up; more Whisper noise phrases; one-time tip to enroll the voice print
(SPEAKER_VERIFICATION=true but no voiceprint.npy). Untested with the user's microphone/speakers.

## Round 26 - master-prompt audit (what exists vs. the 31 sections)

Already built and tested in earlier rounds (not rebuilt): central conversation/orchestrator with validated tool
registry, risk levels, confirmations, taint firewall, standing approvals, emergency stop (s3, s9, s23); model routing,
auto-switching from benchmarks, quality/eval lab, GPU/latency metrics (s6); SQLite memory with categories, provenance,
conflicts, export/delete, semantic + full-text search, knowledge graph, unified search (s7); missions/task engine with
checkpoints, idempotency, retries, rollback, scheduler, file/event triggers, workflow studio + canvas + simulator,
versioning (s8, s18, s19); Windows apps/windows/layouts/UI elements/OCR/vision/screen agent with verification (s4, s10);
Android app + pairing, TLS phone remote, sync, push, wake (s5, s21); context engine/command center/day plan/routines
(s11, s12); to-dos, reminders, goals, study/quiz, notes, mail triage, research (s13, s14); code tools, diff review,
worktree experiments, dev pipeline (s15); diagnostics, troubleshoot, health report, evolution proposals tested before
activation (s16, s17); activity timeline / undo / audit chain (s20); security center, backups, offline mode (s23, s24);
3D control room, talk-over, voice print (s25, s26).

New this round (gaps found): autonomy levels observe/routine/approval enforced in ToolExecutor (s9); duplicate finder and
batch rename with preview, clash detection, two-phase rename and exact undo (s4); local expense records + budgets
(v33, s13). Tests: tests/test_os_upgrades.py (9).

Honest remaining gaps / not verified: real Windows/Android/microphone runs of most UI automation; phone-side features
beyond what the Android app already has (flashlight/notification reading need Android permissions + rebuild); no
calendar conflict UI beyond the planner; habit tracking is basic; no full knowledge-graph UI.

## Round 27 - "build all" (remaining list)

Built and tested (sandbox): database v34 (phone_commands, phone_state, journal, habit_defs, habit_checks).
* Phone link (app/remote/phone_link.py, tool phone_control, server commands phone_commands/phone_result/phone_notify/
  phone_share): PC queues signed-channel actions, the phone collects and reports them; expiry 120 s; a result is
  accepted once and only from the phone it was sent to; OTP-like codes redacted; links http(s) only; the phone must
  opt in. Android: PhoneActions (torch, ring, open app/link, show), NotifyForward (NotificationListenerService),
  ShareActivity (share text to PC), settings card. Android code compiles against JVM stubs (test/run.sh) - NOT run
  on a device; needs a rebuild + install. Tests: tests/test_phone_link.py (6).
* Journal / voice notes (app/core/journal.py, tool journal, direct phrases), habit tracker with streaks and grid
  (app/core/habit_tracker.py, tool habits), expense CSV export (formula-injection safe) + 6-month trend, calendar
  conflicts per day (tool calendar_conflicts, Outlook), knowledge-graph browser (app/ui/graph_browser.py, Knowledge >
  Browse), MENU > My life log (app/ui/personal_dialog.py), `run.py --restart` (app/core/relaunch.py cli_restart +
  window polls a restart request). Tests: tests/test_life_log.py (12).
* Bug found by the new tests and fixed: a bare phone check-in erased the stored battery/control state (now merged).

Not done, honestly:
* FCM receive inside the app: needs Google Play services/Firebase libraries and a Gradle build; the app is built
  with plain javac, so the phone still polls (every 2.5 s while open, background job otherwise).
* Phone screen viewing/control: needs MediaProjection + an accessibility service and a streaming channel - not built.
* Bangla speech/AI data: needs your own recordings/corrections; nothing to train on here.
* Refactor of large functions: not done (risk of regressions without real-hardware testing outweighed the benefit).
* Not verified on real hardware: Outlook calendar reads, --restart on Windows (QLockFile PID read), all Android parts.

## Round 28 - setup wizard showed at every start
Cause (from the logs and .env.bak): Jarvis's settings file (.env) had been replaced by an exact copy of .env.example,
so SETUP_DONE was false again and the wizard returned; Cancel never recorded anything either. The code itself never
copies .env.example over an existing .env (the whole test suite was run with a checksum on .env: unchanged).
Fix: the one-time setup is also recorded in data/setup_state.json (non-secret answers only). At start, a .env that
"forgot" the setup gets SETUP_DONE=true and the remembered answers written back (log event setup_restored), and the
wizard is not shown. Cancel/close also counts as done (Menu > Setup wizard reopens it). Tests: tests/test_setup_once.py.

## Round 29 - master prompt 3 (next-generation ecosystem): audit + highest-impact fixes

Baseline before changes: full suite green except 2 sandbox-only failures (test_v2 needs cv2; one accessibility check
fails only with the sandbox's PySide 6.8.3) - plus one regression from round 28 (taskbar icon colours not using design
tokens), fixed first. The audit compared the prompt's 16 areas with the code; most layers already existed (tool
registry with validation, risk levels, confirmations, taint firewall, missions, scheduler, memory with provenance,
phone pairing with signed requests / nonces / expiry, model capability checks, emergency stop, 3D UI). The gaps fixed:

| Gap found (evidence in the audit) | Fix | Tests |
|---|---|---|
| Phone approvals signed with the base key only | `approve` is a step-up command: YES needs the phone's fingerprint/PIN key, NO never does (app/remote/mobile.py, Protocol.java) | test_master3_security |
| Standing approvals could cover phone requests | grants never cover requests whose run source is a phone (device-scoped) | ditto |
| Emergency stop revoked grants lazily; partial report | revokes grants at once; lists what it stopped and what could not be interrupted (never claims undo) | ditto |
| Only 3 autonomy levels, global only | `suggest` level (plans, does nothing); AUTONOMY_REMOTE (stricter applies to phone requests); per-category ask / block lists; applied live | ditto |
| Silent model substitutions (unhealthy / too big / no tools / saved model gone / auto-switch) | each is told once in the answer, then logged | ditto, test_model_health |
| Missed recurring reminders skipped silently; no sleep handling | "Missed ... skipped, not run late" notices; a >3 min scheduler gap = PC slept: catch-up without a burst | test_automation |
| No memory edit / import / "why retrieved" | manage_memory edit (keeps revision), Memory window Edit button, validated import, query-aware prompt memories with recorded reasons + `memory_used` tool | test_master3_memory |
| No microphone mute; no TRANSCRIBING state | Ctrl+M / menu / tray / voice mute that closes the stream; MIC MUTED + TRANSCRIBING badges | test_master3_voice |
| Phone could only emergency-stop | `cancel` command (chat permission) + "Cancel what Jarvis is doing" button | test_master3_security |
| No capability / offline matrix | app/core/capabilities.py, `python run.py --capabilities`, docs/CAPABILITIES.md (kept in sync by a test) | ditto |

Verification status (honest):
* Implemented + tested automatically (sandbox, Linux, mocks where hardware is needed): everything in the table.
* NOT verified on your Windows PC yet: the mic mute closing the real microphone stream, the TRANSCRIBING badge timing,
  the sleep/resume reminder behaviour after a real Windows sleep.
* NOT verified on a real phone: step-up approvals and the Cancel button need the rebuilt JARVIS Mobile
  (packaging\\build_android.ps1). An old phone app can still say NO to approvals; YES needs the updated app.
* Not done this round (larger, later stages of the prompt): per-claim source labels in replies, model benchmarking
  across roles on your hardware, workspace screens redesign, smart-home adapters beyond Home Assistant, Bangla model
  data. These need your hardware/data or are big UI projects; the existing equivalents stay as they were.

## Round 30 - the open items from round 29

| Item | Built | Tests (sandbox; hardware simulated where noted) |
|---|---|---|
| Per-answer sources | `answer_sources()` from the real executions + matching memories; "Sources: ..." notice (SHOW_SOURCES); `answer_sources` tool ("where did you get that?") | test_master3_sources |
| Model roles from facts | app/ai/roles.py (capabilities, RAM fit, health, YOUR benchmark; no name-based judging); `--model-roles [--apply]`; MENU > AI and models (table, recommended jobs, apply with confirmation, set main model) | test_master3_models |
| Local smart home without a hub | app/core/local_home.py: Shelly Gen1/Gen2+, Tasmota, WLED over their documented local HTTP APIs; private addresses only; read-back verification; per-device "ask first"; `local_device` tool | test_local_home (HTTP simulated with httpx.MockTransport) |
| Home Assistant honesty | home_control re-reads the entity: "confirmed" only when HA reports the new state, else not_verified | test_local_home, test_v22 |
| Device manager workspace | MENU > Device manager: computers, phones (battery / last check-in / control allowed, revoke), network devices (check, add, remove) | test_device_manager |

Still not possible here, honestly: Bangla speech/AI training data needs your own recordings or corrections (the
tools to collect them exist: `--record-benchmark`, `--train-voice`); a full multi-workspace redesign of the main
window was not done (the new windows join Security center, Mission control, Workflow studio, My day, Life log,
Knowledge, Notifications and Settings, all in the MENU). Not verified with real devices: Shelly / Tasmota / WLED
hardware, Ollama on your PC for --model-roles, the rebuilt phone app.

## Round 31 - learning from every request + measured self-improvement

* app/agent/growth.py + DB v35 (learning_turns, growth_items): every request recorded with its real tool results;
  outcome settled by the NEXT event (failure / "no" / correction / repeat = bad; moving on / 2 quiet minutes = good);
  skills (request -> tools that worked) and lessons (mistake -> what was wanted) injected for similar requests;
  per-item effect scoring and automatic retirement; weekly success-rate report. Hooked into ConversationManager.send.
* app/agent/self_improve.py + improve_job.py: personal test set from real use; candidates = current base, a more exact
  temperature, other installed tool-capable models that fit RAM; switch only on a measured gain (min 8 cases,
  +5 % or same score and 20 % faster, no more malformed calls); history file; training-data export.
* Tools: learning (report / lessons / forget), tune_ai (background, asks first); improve_yourself now reports the loop.
* Tests: tests/test_growth.py (9, incl. a real ConversationManager turn). Not run against real Ollama models here:
  `--self-improve` scoring needs your PC with Ollama. Weights are never retrained (that needs a GPU trainer;
  --export-training prepares the data).

## Round 32 - FULL_POWER (user request: "give it unlimited power, just ask me")

* settings.full_power (FULL_POWER, turned on in the user's .env): CRITICAL decisions become an on-screen confirmation
  instead of a refusal (app/agent/tool_manager.py, `_full_power_allows`): only for text/voice/cli sources; summary
  prefixed "CRITICAL"; voice answers refused (voice.py), phone approvals refused (remote.py); never covered by direct
  commands, mission pre-approval or standing grants (those are MODERATE-only already).
* run_command's defence-in-depth block honours full power; Evolution proposals may touch the security core in full
  power (risk "critical" -> apply_proposal is CRITICAL = on-screen approval); .env is never editable.
* Unchanged on purpose: automations/agents/phones never get critical actions; taint firewall; emergency stop; audit.
* Tests: tests/test_full_power.py (7). Full suite green except the 2 sandbox-only failures.

## Round 33 - start-up crash (native access violation after the window appeared)

* Cause (from crash.log + jarvis.log): crashes only on visible starts (intro -> window fade-in) while Ollama and the
  CUDA speech model load on the 6 GB GPU; tray starts worked. Fixes: no whole-window opacity fades on the OpenGL window
  (`MainWindow._can_fade`), the intro closes before the window shows, GL state reset after native painting
  (`GlOrbWidget._reset_gl_state`), GL vendor / first frame / stable logged.
* Crash guard app/ui/core3d/gl_guard.py: a crash between "window shown" and "GL stable" steps the next start down
  (no intro/fades + balanced, then the flat core); `python run.py --reset-gl-guard` restores. tests/test_gl_guard.py (6).
* NOT verified on the user's NVIDIA/Windows PC - needs a few real starts.

## Round 34 - "build all" (the open items)

| Item | Built | Verified here |
|---|---|---|
| Core extras | Interface sounds made on the PC (UI_SOUNDS, app/ui/sound_fx.py; silent while speaking/listening); the speaking animation follows the REAL voice loudness (TTSManager.speech_level); data pulses core -> panel (CORE_PULSES) and spark trails (CORE_TRAILS) (app/ui/core3d/fx.py, app/ui/dashboard_fx.py); HUD_SCALE (whole window, next start); HIDDEN_PANELS | tests/test_core_extras.py; GL render checked under Xvfb + Mesa (software GL) |
| Instant phone updates, no Firebase | signed read-only `wait_events` long poll (server holds it up to 25 s); JARVIS Mobile keeps one open while the app is open (LiveEvents.java), polling stays as fallback | tests/test_live_events.py; Java compiled + JVM test (SyncClientTest) |
| Phone screen on the PC | JARVIS Mobile "Share screen with PC" (MediaProjection, Android asks every time, notification while sharing) -> /api/mobile/frame (signed, own rate bucket, 450 KB cap, memory only); PC viewer MENU > Phone screen / "show my phone screen"; taps/swipes/Back/Home/Recents/text only with the phone's "JARVIS control" accessibility service AND "Allow the PC to tap"; Stop on either side; emergency stop ends it | tests/test_phone_screen.py (incl. a real signed frame through the server); ALL Android sources compile against compile-only stand-ins (test/compile_all.sh) |
| Bangla / your voice | SPEECH_DATASET (off/corrections/all): misheard audio + your correction kept as training data (data/speech_dataset); Bangla recognition hints from your Bangla corrections; `voice_data` tool + `--export-speech-dataset`; scripts/finetune_whisper.py (GPU, WER before/after, keeps nothing if not better) -> STT_BANGLA_MODEL | tests/test_speech_dataset.py |
| Retraining the AI | `--export-training` now includes the offered tools; scripts/finetune_llm.py (unsloth LoRA -> GGUF -> `ollama create jarvis-tuned` with the base's template); self-improvement tries jarvis-tuned first and switches only on a measured gain | tests/test_finetune_pipeline.py (pure parts only) |
| Workspaces | MENU > Workspaces / Ctrl+K / "show my workspaces": every screen grouped (Today, Control, Intelligence, Build, Safety and system), type to filter | tests/test_workspaces.py |
| Refactor | match_shortcut split into 6 ordered stages (identical results on 15,076 phrase/variant checks against the old code); ToolExecutor.execute split into _prepare / _gate / _autonomy / _skip_confirmation_when_allowed / _ask / _run_checked (same order, same audit events) | full suite |
| Dependency audit | pip-audit of all 109 packages in the PC's .venv (incl. pywin32, pycaw, comtypes, winrt-*): no known vulnerabilities (2026-10-18) | - |
| Fixed on the way | emergency_stop called `tts.is_speaking()` (a property) so "speaking" was never listed as stopped; tests/test_android_sync_client.py compiled too few files (it was always skipped without a JDK) | - |

NOT verified on real hardware: everything on the phone (screen sharing, accessibility taps, live updates - rebuild the
app with packaging\build_android.ps1, version 1.1.0), sounds/pulses on your GPU, the two GPU fine-tune scripts.
Sandbox-only: tests/test_window_overview.py crashes the sandbox's Qt; test_v2 voice test needs piper.

## Round 35 - full phone control (JARVIS Mobile 1.2)

* Why "Phone screen" stayed empty: the phone still runs JARVIS Mobile 1.0, which has no "Share screen with PC" (its
  "See the screen" shows the PC's screen on the phone). The PC side was waiting correctly. Fix: rebuild + install 1.2.
* New phone actions (phone_control): call (contact name or number; ACTION_CALL with the Phone permission, else the
  dialer), end_call, send_sms, read_messages (latest 1-10, optional sender, one-time codes hidden on the phone),
  find_contact, open_gallery / open_camera / open_messages / open_dialer, share_screen (opens Android's own consent).
  Rules: a separate phone switch "Allow calls and messages" + Android permissions; call/send_sms are DANGEROUS
  (always asked, on screen, with number and text); read_messages / find_contact MODERATE; USSD/service codes (* #)
  refused on both sides.
* "Stay connected" (LinkService, opt-in foreground service with a notification): the PC's requests run within ~3 s
  while the app is closed. Opening apps / calling from the background needs the "JARVIS control" accessibility
  service on (Android's rule).
* PC viewer: "Ask the phone to share" button. Tests: tests/test_phone_comms.py; all Android sources compile against
  the stand-ins; JVM test passes. NOT run on a real phone.

## Round 36 - master prompt 4: audit, then the highest-value P0/P1 gaps

Audit (code, not docs) of 18 areas: Wake-on-LAN complete (sent / PC answers / Jarvis ready); 15 partial; 2 missing.
Built this round (all with tests):

| Gap found | Fixed | Tests |
|---|---|---|
| A running mission could not really be paused or cancelled (Esc paused it; "cancel the mission" was refused while busy and didn't stop the loop); a mission stuck in "verifying" was never recovered | missions.request(): pause/cancel at the next step boundary (a started step finishes; a retry yields to it); cancel_mission on a running mission asks it to stop; restart recovery includes "verifying" | test_live_control.py |
| "stop speaking", "pause this mission", "cancel the current task", "explain what you are doing", "show me the plan", "do not remember this conversation" missing / refused while busy | app/core/live_control.py (EN + BN), handled by the window even while a request runs; "don't remember" = private mode | test_live_control.py |
| Phone protocol: no version, no idempotency, queued PC->phone actions couldn't be withdrawn, no 'unsupported' | envelope "protocol" (absent = 1, unknown = 426); a repeated request_id returns the first answer (non-read commands, 10 min); phone_control cancel_pending + "cancel the phone request" + emergency stop withdraws waiting ones; 'unsupported' status from older apps; schema v36 (table rebuilt, rows kept) | test_phone_protocol.py |
| Automations: a file-watch automation writing into its own folder re-fired itself; no rate cap; quiet hours ignored | re-baseline after the run; MAX_EVENT_RUNS_PER_HOUR=12 per event-started automation (reported once); AUTOMATION_QUIET_HOURS (off by default; scheduled ones still run) | test_automation_guards.py |
| Notifications: no de-duplication, no age retention | the same unread notice within 30 min isn't added or shown twice; read notices older than 90 days removed (unread stay) | test_master4.py |
| Security center showed settings only | "Right now": microphone (muted / wake word / recording), screen capture, phones sharing their screen, remote listening, the approval waiting, mode | test_master4.py |
| Daily briefing not configurable | BRIEFING_LENGTH (short/normal/full), BRIEFING_SECTIONS; new parts: unfinished mission, unread notifications | test_master4.py |

Not done this round (honest): PLANNING / PAUSED core-animation states (the core shows the real agent states it has:
ready, listening, recognizing, thinking, executing, waiting for permission, speaking, error); a per-memory approval
column and a "project" memory type; page/section references and stale warnings in document answers; clipboard
sharing PC<->phone and phone->PC file upload from the app; a combined performance baseline command (startup,
model connect, first reply are logged separately: window_ready, model_warmed_up, voice_stats); automatic rollback
after an applied self-improvement (rollback stays one approved command). Only 12 of ~225 tools have a verify step.
Not verified on real hardware: everything phone-side, the security center on Windows.

## Round 37 - "it said WhatsApp is open, but nothing opened"

* Cause (jarvis.log 20:59): every reply in that exchange had tools_used = [] - the AI (jarvis / qwen3:4b) only SAID
  "Opening WhatsApp now" / "WhatsApp is open"; no action ran. A false success.
* app/agent/honesty.py + ConversationManager._keep_words_and_deeds: a reply that claims an action with no tool call
  either really does it (through the executor, as the model's choice, so risky actions still ask) or gets an honest
  "(Correction: no action actually ran ...)". "Do it" / "yes" / "I'm ready" / "হ্যাঁ" right after Jarvis offered or
  claimed an action now runs that action directly instead of going back to the model.
* open_application now waits for the app's window (Store apps take a few seconds) and brings it IN FRONT, checks it,
  and says so if Windows kept it behind; stepping aside without the fade (OpenGL window) now also brings the app forward.
* Tests: tests/test_honesty.py. Not verified on Windows: the foreground behaviour itself.

## Round 38 - "build all" (the idea list), JARVIS Mobile 1.3

Many items of the idea list already existed (UI-element clicks, browser automation, Office documents, duplicates,
PC health / troubleshoot, journal / habits / expenses, flashcards, meeting notes, model roles, voice verification,
audit chain, emergency stop, focus mode, phone remote keys). Built new this round (all with tests):

| Built | Where | Tests |
|---|---|---|
| Phone screen as text + tap by text (accessibility tree, passwords never read, protected apps give nothing) | ControlService.readScreen/tapText; phone_control read_screen / tap_text (MODERATE) | test_phone_v13.py |
| Two-way clipboard: PC -> phone (set_clipboard), phone -> PC ("Send my clipboard to the PC") | PhoneActions, phone_clipboard command | test_phone_v13.py |
| Photos / files from the phone (Share > JARVIS Mobile) -> data\phone_inbox (signed /api/mobile/upload, 25 MB, 300 MB/day, never overwrites, never runs) | ShareActivity, app/remote/phone_inbox.py | test_phone_v13.py (real signed upload) |
| Where is my phone (last known location, only to your PC, after you allow Location) | PhoneActions.locate | test_phone_v13.py |
| Expenses from bank / bKash / Nagad SMS: preview, import, never twice, OTPs / credits skipped; the phone no longer hides amounts (only messages with a code are masked) | app/core/bank_sms.py, import_bank_sms | test_bank_sms.py |
| Clipboard AI: summarise / translate / fix / explain / reply / shorten what you copied, result back on the clipboard (local model; secrets refused) | app/tools/power_tools.py clipboard_ai | test_power_tools.py |
| Excel through Office's own COM: sheets, read, write, append row, create; writes ask; '=' values are text unless allowed (formula injection) | power_tools.excel_control | test_power_tools.py (fake Excel) |
| Phone low-battery alert on the PC (once per drop below 15 %) | PhoneLink._battery_alert | test_power_tools.py |
| Command palette: Ctrl+K - no matching screen + Enter = run it as a command | app/ui/workspaces.py | test_power_tools.py |
| Document answers cite the PDF page and warn when a file changed since indexing; files named like passwords / keys and AppData / .ssh are never indexed | app/memory/semantic.py, tools/knowledge.py | test_docs_pages.py |
| Bangla digits in direct commands ("ভলিউম ৫০") | ConversationManager._shortcuts | test_docs_pages.py |

Not built (honest): teach-by-showing recorder (recording your input globally is keylogger-like; workflows can be
saved from what Jarvis did instead), phone-side automations (geofence / charging triggers), automatic swapping of a
bigger "thinking" model, phone lock from the PC (needs Android device-admin). Nothing on the phone
has run on a real device yet: rebuild JARVIS Mobile 1.3.0 (packaging\build_android.ps1). Excel automation and
foreground handling are untested on Windows.

## Round 50 - reusable skills with named inputs (`docs/skill-inputs.md`)

Workflows can declare up to 5 named inputs (`{{site}}`) that are filled in when run (`run_workflow params={...}`, else the saved
default). Values are validated (text/number, <=300 chars, no control characters, no template text); only ARGUMENT text is
substituted - never a tool name, step type or permission - and every step still goes through the normal permission checks. The
Experience Compiler now turns text that changed between your repeated runs into inputs (numbers, lists and secret-looking text stay
fixed and are reported). Migration 40 `workflow_parameters`; replacing a workflow keeps its inputs if the steps still use them; deleting
removes them. 9 tests + 3 lab cases. NOT built: editing inputs in the UI panel, typed (number/choice) inputs, inputs for scheduled
automations' notify messages, and none of it has run against real apps on your PC.

## Round 49 - measured model comparison (`docs/model-benchmark.md`)

You chose "stronger local model, measured" and "learn from its own history". Audit: both were mostly built already
(`--eval-quality` with latency percentiles and GPU sampling, personal test set from real use in `growth.cases()`, `self_improve`,
LoRA export). Real gaps found and built: no tokens/second, load-time or first-response measurement, no priority (speed / quality /
low resource / balanced), and model choice never looked at GPU fit. New `app/ai/modelbench.py` + `python run.py --benchmark-models
[--priority ...]`: unloads each installed model, measures Ollama's own timings, GPU share (`/api/ps`) and GPU memory peak, stores them,
and recommends per priority - never a model without a quality benchmark; each failed safety question costs 5 points and is always shown; models only partly on the GPU are left out of speed / balanced / low-resource picks; unmeasured values are '-' not zero.
10 tests + 2 lab cases. NOT done: it has never run against a real Ollama/GPU (needs your PC); no fine-tuning was run (needs a GPU
and your accumulated history); quality of any model on YOUR requests is only known after `--eval-quality`.

## Round 48 - gap map and the time-travel task debugger (`docs/FEATURE_GAP_MAP.md`, `docs/replay.md`)

Feature-by-feature gap map (A-G) from repository evidence; the top genuinely missing feature, the task debugger, is built:
structured per-call timeline from the existing audit trail, recorded/reconstructed/not-recorded labelling, earliest divergence,
minimal recovery plan, simulation replay (real mission engine + mock executor), live re-run of read-only calls only, hidden-tool
list, `task_replay` tool and panel. 16 tests + 3 lab cases. NOT built (see the map): tool foundry (generation from a request),
workspace objects, retrieval source adapters, performance optimizer (needs your hardware), multilingual dataset, more diagnostic
hypothesis sets. Nothing run on real hardware.

## Round 47 - goal-verified missions and skill reliability (`docs/goal-verified-missions.md`)

Audit against the "Jarvis 4.0" prompt: nearly everything it lists exists already (policy engine, handoff, offline guard,
world model, lab, command center, experience compiler). Two confirmed gaps built: (1) goal contracts were not used by
mission execution - a mission was "completed" once its steps finished. Now the title's contract is checked against what the
steps really did: a directly contradicted goal pauses the mission, an unconfirmable one completes with an explicit note, and a
plan that breaks what you said ("don't delete anything", "offline") is refused before it starts; (2) saved workflows had no
reliability history: migration 39 `skill_runs`, 3 failures in a row suspend (never delete) a skill with the reason, refusals and
emergency stops don't count, re-enabling resets the streak, the listing shows the success rate. 10 tests + 4 lab cases.
Still NOT built: parameterised skills (varying arguments are reported, not templated), the Android handoff screen, anything on
real hardware; the goal check relies on the heuristic criteria in goal_contract.py, so it can say "could not confirm" for goals it cannot parse.

## Round 46 - anomaly detection and the Experience Compiler (`docs/anomaly-and-experience.md`)

Built the two items round 45 listed as not built: (1) statistical anomaly detection (median/MAD, binomial tail) over tool
latency and failures, PC load and spending, feeding the radar and a read-only `anomalies` tool; (2) an Experience Compiler that
mines routines you repeat (3+ runs, 2+ days) from Jarvis's own audit trail and proposes workflows - never saved without an
on-screen confirmation, never including risky, failed, secret or forbidden calls. Audit rows now record `elapsed_ms`.
17 tests + 6 lab cases + two Intelligence panels. NOT built: the phone-side screen for handoffs (needs an APK release);
anything run on real hardware; latency anomalies only exist for calls made after this version.

## Round 45 - strict offline guard and task handoff (`docs/offline-and-handoff.md`)

Audit against the "personal AI ecosystem" spec: nearly everything already exists (see earlier rounds). Real gaps built: (1) strict
offline is now enforced at the socket (no connection or DNS query leaves the PC/LAN with OFFLINE_MODE=on), shown in the Security
view with a proxy warning - the first test run found that a configured proxy forwards traffic past it, so that limit is stated;
(2) cross-device task handoff: signed, expiring, single-use, device-bound, checked against the mission's real state, secret-free,
migration 38. NOT built: the phone-side screen for handoffs; a Personal Experience Compiler beyond the existing safe
teach-by-showing; per-step statistical anomaly detection; a second implementation of anything that already exists. Nothing here
was run on a real phone or Windows PC.

## Round 44 - the rest of the intelligence list (`docs/intelligence.md`)

Built: decision records + option comparison, preference learning with profiles and precedence, active information gathering
(model-failure investigation), knowledge synthesis (sourced offline briefs, gaps, proposed links), long-horizon goal review,
radar signals + feedback, capability discovery, 7 tools, DB migration 37, and an Intelligence page in the command center (11th
rail section, concise / full-detail). Found and fixed while testing: the "never ask me" refusal missed "stop asking me". NOT
built or only partial: nothing records observed preference corrections yet (the API exists); only one investigation topic;
no semantic search in briefs; not seen on your screen; no model is used anywhere in this round.

## Round 43 - goal contracts (`docs/goal-contract.md`)

Audit against the intelligence spec: there was no code that states what would count as "done" for a request, no evidence
ledger and no decision records. Built the first: `goal_contract.py` (compile -> constraints, criteria, assumptions, material
questions, conflicts; check_plan; evidence-based evaluate) + read-only tool `goal_check` + 7 Reliability-Lab cases. Whole suite
passes in chunks (Linux, Python 3.10, offscreen). NOT built (not started, not claimed): counterfactual/decision records,
preference learning engine, active information gathering, knowledge synthesis, long-horizon goals, problem radar,
capability discovery, resource scheduler beyond what exists, digital simulation (the automation simulator exists), Intelligence
Center page. It is not yet applied automatically to every request or mission; no model is involved.

## Round 42 - reliability lab (`run.py --lab`, `docs/reliability-lab.md`)

Chosen after inspecting the code: task recovery, model evaluation (`--eval-quality`), goals, health signals and teach-by-showing
already exist; what was missing was one place that checks permissions, recovery, phone replay protection, memory recall and
command matching together, counts incorrect actions, tracks regressions, and labels simulated vs hardware. 20 cases pass here;
sabotage tests prove it fails when a rule is broken. Not built (already covered or needs hardware): cross-device mission
resume, a digital twin, predictive health, a new demonstration recorder. The CLI path was only checked through the argument
parser (the sandbox has Python 3.10; run `python run.py --lab` on your PC).

## Round 41 - the command-center view (app/ui/command_center/)

A new main working view BESIDE the 3D core (nothing removed): MENU > Command center, Ctrl+Shift+C (back: Core view,
Ctrl+Shift+H), also in Workspaces. Built on first use, replays the chat, and uses the same controller, so both views show
the same conversation. Pure view-models (testable without a screen) + thin widgets:

| Part | What it does | Where |
|---|---|---|
| Left rail | Chat, Dashboard, Tasks, Devices, Memory, Knowledge, Automations, Developer Tools, Settings, Security; Alt+1..0 | widgets.py |
| Ten states | Idle, Listening, Thinking, Executing, Needs approval, Verifying, Completed, Failed, Offline, Emergency stop - each a shape + a word + a colour; fixed priority; Verifying is real (`app/core/activity.py`, set while a tool's check runs) | states.py |
| Status area | state, local model, CPU / RAM / GPU (a dash when not measured, never 0), mic, network mode, devices connected | widgets.py, view.py |
| Command bar | type / Talk / Send / Stop, Ctrl+L, execution status line | widgets.py |
| Context panel | Task timeline (done / running / waiting / failed, device per step, errors) with Pause / Cancel / Retry / Undo / Review shown ONLY when the task supports them; what the last answer used; devices; execution details | timeline.py, context_panel.py |
| Approvals | the confirm dialog now shows target + risk in words + the real arguments before you answer | approval.py, confirm_dialog.py |
| Devices | PC + phones: connection (connected / recently seen / offline / waiting for Trust), battery only if reported, permissions, per-capability available / needs action / unknown with the missing step, last check-in | devices.py |
| Security | phones and permissions, mic / screen state, local-data settings, standing approvals, blocked + sensitive action history, backup health, activity-log check, Emergency stop button | security.py |
| Automations | Trigger, Conditions, Actions, Approvals, If it fails, warnings (private data / changes the PC / runs by itself); Test (dry run, nothing runs); Simulate what-if; Enable refused with problems, needs a tick with warnings | automations.py |
| Look | dark / light (`UI_THEME`), text size 0.8-1.6 (`UI_TEXT_SCALE`), reduced motion (existing), focus ring, tooltips, accessible names; contrast of both palettes tested | style.py |

Knowledge page (added after the audit, `graph_view.py` + `KnowledgePage`): browse the knowledge graph by kind, search, see each item's
connections with their evidence, and ask graph questions; the graph is read from data already on this PC in a background thread.

Honest limits: not seen on a real screen or with a screen reader (checked offscreen + the accessibility audit); the chat
bubbles stay dark in the light theme (ChatView paints them); editing steps still opens the Workflow studio; the HUD's
look is unchanged. The timeline reads missions; one-off requests show in Details.

## Round 40 - the four "not built" items, JARVIS Mobile 1.4

| Built | Where | Tests |
|---|---|---|
| Lock the phone from the PC. Only after YOU activate JARVIS Mobile as a device admin on the phone (Android's own screen opens); the only policy is `force-lock` (no wipe, no password rules). Always asks on screen (DANGEROUS) | LockAdmin.java, res/xml/device_admin.xml, phone_control `lock_phone` | test_phone_v14.py |
| Phone-side triggers for automations: "when my phone is plugged in / unplugged", "when my phone's battery drops below N%" (`when_event` phone_charging / phone_battery_low). They use the phone's check-ins, so they fire while the app is open or "Stay connected" is on | phone_link.LATEST, scheduler `_phone_due`, triggers.py | test_phone_v14.py |
| Hard questions use a bigger installed model when it FITS ENTIRELY in GPU memory (`AI_AUTO_SMART`, on by default; never when the GPU can't be read; never an embedding or no-tools model); the first use is told with the answer | app/ai/thinking_swap.py, manager `_bigger_that_fits` | test_thinking_swap.py |
| Teach by showing, the safe version: "teach routine X" -> tell Jarvis what to do -> "done teaching" saves what Jarvis actually ran as a workflow (asks first, shows the steps). It never records your keyboard, mouse or screen (that would be a keylogger); secrets, risky tools and tools automations may not use are left out | app/automation/teach.py, tool `teach_workflow` | test_teach.py |

Honest limits: nothing phone-side has run on a real device. On a 6 GB GPU a bigger model rarely fits next to speech, so
the swap will mostly stay idle there (it never trades speed for size). The teach recorder cannot learn things you do by
hand; it learns what you tell Jarvis to do. App version 1.4.0 (rebuild and install it).

## Round 39 - encrypted USB backup

`app/memory/encrypted_backup.py` + `scripts/encrypted_backup.py`: a database snapshot (SQLite backup API, integrity
checked) encrypted with your password (scrypt + AES-256-GCM) into `jarvis-<time>.jbk` on any drive. The password is never
stored (lose it, lose the file). `open` decrypts to a new .db, checks it, never overwrites. Usage:
`python scripts\encrypted_backup.py save E:\` / `open E:\jarvis-....jbk restored.db`; then restore with the existing
restore path. Tests: tests/test_encrypted_backup.py (round trip, wrong password, tampering). Not done: a button or voice
command for it, and only the database is covered (not `.env` or phone keys, deliberately).


## Round 51 - hearing quality
Audit of the voice-input path. Fixed: no anti-alias filter when the mic is not at 16 kHz (new `StreamResampler`), per-chunk resampling
seams in hands-free streaming, DC/rumble reaching the recogniser (`remove_rumble`, 80 Hz zero-phase high-pass), click-dominated level
gain (percentile-based), short pre-roll (0.3 -> 0.45 s). Tests: `tests/test_hearing_quality.py` (7) plus the existing voice suites pass
(2 env-only failures here: Qt/OpenGL). NOT verified on a real microphone: compare `--benchmark` before/after on your PC. See docs/voice.md.

## Round 52 - digital twin and what-if (master prompt section 12)
Audit result: of the master prompt's sections, missions/state machine, memory, model routing, computer control, Android/WoL (staged
wake), security kernel, events, backup, self-improvement lab, replay, anomaly detection and the command center already exist and are
tested; the genuine gap was the digital twin with staleness and the pre-run what-if preview. Added migration 41 `twin_observations`,
`app/core/twin.py`, tool `digital_twin` (+1 tool), Intelligence page "Digital twin", lab suite, `docs/digital-twin.md`,
`tests/test_twin.py` (10). Still open (unchanged): tool foundry, workspace objects, retrieval adapters, Windows/phone/GPU/mic
verification on real hardware, rebuilt APK, leaked-key decision.


## Round 53 - foundry, workspaces, retrieval sources, multilingual set, APK/key helpers
New tools `build_tool`, `workspace`, `retrieval_sources`. New: `app/core/foundry.py`, `app/core/workspaces.py`, `app/memory/sources.py`,
`app/agent/multilingual_eval.py`, `app/remote/apk_check.py`, `scripts/signing_key.py`. Fixed in `language.py`: negated Bangla commands were rewritten into the
command (safety), "অ্যাপ" inside app names (WhatsApp), Unicode normalisation. Combined search now skips near-copies. Tests: foundry 9, workspaces 7, sources 8,
multilingual 6, apk 7; lab suite "multilingual". Not verified on real hardware: workspace restore on Windows, Bangla speech recognition. APK: bundled 1.4.0 matches
source (not rebuilt here - no Android SDK); signing key still in two history commits, decision left to you (`python scripts/signing_key.py plan`).


## Round 54 - screen watch + master-prompt audit
Audit table in `docs/MASTER_AUDIT.md`. Built the one missing capability: `core/screen_watch.py` + tool `screen_watch` (watch a window/screen for a change or a phrase:
explicit and asks first, <=3 watches, 30 min default / 4 h max, blocked-window list applied on every look, capture indicator on while running, adaptive backoff,
OCR only after a change, alert cooldown, stops on emergency lock / `stop_all`). Tests `tests/test_screen_watch.py` (11, injected observations). The real capture path
(`tools/screen_watch_tool.py::_observer`) is Windows-only and HARDWARE-UNVERIFIED. Not measured (pending hardware): end-of-speech and interruption latency.

## Round 55 - on-PC hardware validation
`python run.py --validate windows|voice|phone|all|status` (`app/validation/*`, `docs/hardware-validation.md`): Windows UIA / DPI / monitors / focus, voice detector reaction +
end-of-speech + speaker-stop + self-hearing, phone real actions confirmed by you; PENDING is never a pass. Benchmark now reports word/character error per language (Bangla, mixed,
English) with a no-claim rule under 8 recordings, plus 14 recognition-only prompts. 25 tests with fake hardware. NOT run on real hardware by me: RealEnv / RealVoiceEnv / RealPhoneEnv.

## Signing-key decision (28 Oct 2026)
Decision by the owner: the GitHub repository has always been private, so the existing Android signing key is KEPT (no rotation, no history rewrite). Residual risk, accepted:
two commits (`79d7065`, `c90b319`) still contain the key and `password.txt`; anyone who ever had read access, a fork or a clone has them. Revisit (`python scripts/signing_key.py plan`,
`new-key --yes`) if the repository is ever made public, shared, or a copy leaves your control. Keep `android/signing` and `*.jks` out of git (already in `.gitignore`).

## Round 56: hand-gesture control (28 Oct 2026)
`app/gesture/*`, `app/tools/gesture_tool.py`, `docs/gestures.md`, 42 tests. Shared camera broker (presence now leases it). Off by default; camera only on explicit enable; fail-closed safety controller; emergency stop wired into the existing stop.
NOT verified on real hardware: MediaPipe detection, real mouse injection under DPI/multi-monitor. UI dashboard/preview/calibration wizard and Android gestures not built.

Round 56b: right click (thumb+middle pinch, off by default), cursor over the whole monitor, voice shortcuts, Gestures page (panels, skeleton preview, calibration wizard, tutorial, STOP),
`--validate gesture` (wheel/model/DPI/pointer round-trip/camera/real detection). 54 tests. Qt page and all real-hardware checks still need to be run by you.

Round 56d: "what are you seeing in my screen?" (any wording, filler or swearing) now LOOKS (describe_screen) instead of going to the chat model; a screenshot file is made only for "screenshot".
New screen-assist mode: `screen_watch` condition=assist ("watch my screen and help me", "stop watching my screen"): a local vision model looks only after the screen settles (cooldown 45 s), speaks only when
it has a useful tip, visible capture indicator, blocked windows skipped, stops by itself after 30 min. Needs a vision model (`ollama pull qwen2.5vl:3b`). Not run against a real vision model here.
Gesture commands also accept speech mishearings ("jaster control on", "J star control on"); command words added to the recognizer hints.

Round 56e: two-hand detection, two-hand pinch zoom (Ctrl+wheel), open-palm swipe (opt-in actions), dwell click (opt-in), Midas-touch guard, pointer-hand continuity, new settings/tool/page options, tutorial steps. 73 gesture tests. Hardware feel not verified.

Round 57: audio output switching (`audio_output`): speakers / headphones / Bluetooth / connected / named device, PC-speaker fallback, loud option, result verified by reading Windows back. `app/core/audio_route.py` (tested with fake devices), `app/tools/audio_win.py` (IPolicyConfig via comtypes - NOT run on real devices).

## Round 58 - master-prompt audit and first implementation slice (29 Oct 2026)

Audit result: most of the master prompt already existed (single ToolExecutor path, approvals, replay protection, phone protocol versioning,
twin, foundry, workspaces, memory provenance, offline guard). Baseline in the Linux sandbox: only the Qt/libEGL tests cannot run
(about 30, same set before and after); no JDK here, so the Android JVM tests were NOT run.

Fixed / built (all unit-tested):
| Item | What |
|---|---|
| Android test harness | `test/run.sh` did not compile `LockAdmin.java` (needed by `PhoneActions`); added + `tests/test_android_harness.py` guards it. Still needs a real `javac` run. |
| goal_contract clock bug | past-date check used the real clock, not the `today` given: fixed |
| Action lifecycle | `app/core/action_record.py`: validated states, approval bound to a digest of tool+args+target+mission+risk with expiry, UNCERTAIN end state, idempotency key; driven by `ToolExecutor`, mirrored to the audit log (`action_state`) |
| Retry after uncertain | an identical MODERATE+ action that ended UNCERTAIN always asks again; queued-but-unanswered device requests end UNCERTAIN |
| Phone capabilities | `app/core/device_capabilities.py` + `phone_control capabilities`: declared vs granted vs demonstrated, with the exact fix and an alternative |
| Gesture bugs | mapped gestures never ran (called from the camera thread); one noisy frame could click (`pinch_confirm=2`) |
| Gesture profiles / macros / metrics | `profiles.py` (6 profiles, app suggestion, opt-in auto), `macros.py` (persisted, profile-scoped, triggers SWIPE_LEFT/RIGHT/FIST only, risk cap), `metrics.py` (seeded synthetic benchmark: old rule 3 false activations, new 0) |
| Performance modes | `app/core/perf.py` + `performance_mode` tool: Fast/Balanced/High quality/Battery saver/Auto from measured RAM/CPU/GPU/battery; drives model-role choice, auto bigger-model swap, backup/research deferral (max 3 days), gesture fps/hands; `fit` explains with numbers |
| Fault injection | reliability-lab suite "injected faults" (9 cases) |

NOT verified: anything on real hardware (Windows, phone, camera, GPU); Android JVM tests; the Gestures-page profile selector (Qt, syntax-checked only).
Not built yet: unified cross-device mission handoff UI, work-state snapshots/resume screen, personal data refinery, finance/wellness workspaces, command-center redesign, TLS/pairing review.


## Round 59 (command center, handoff, snapshots, personal data)
- Built and unit-tested (pure logic): work_snapshot (save/resume without replaying uncertain steps), finance_plan, data_refinery (nothing trusted until confirmed), wellness (opt-in, off by default), dashboard_data, handoff_data. TLS key at rest is now encrypted with a DPAPI-protected passphrase (migrates existing keys). GPU preference setting (`GPU_PREFERENCE=nvidia`).
- Written but NOT run here (needs Qt + a display): Dashboard cards page, Handoff page, `forest` theme (contrast verified by test; look not seen), mockup `docs/command_center_forest_mockup.html`.
- Not verified: real devices, Android JVM tests (no JDK), the ~30 pre-existing Qt tests that cannot start in the sandbox.


## Round 60 (Stage 3: unified personal intelligence core)
- Built and unit-tested: `core/request_context.py` (evidence-labelled, budgeted, fresh-aware context for every request, plus ask-instead-of-guess), `core/blackboard.py` (shared per-request agent record; blocks repeated side effects, collisions, delegation loops), tool `request_context`, wired into the conversation prompt and the orchestrator.
- Also this round: fixed the audio switch (wrong COM class id; verified from your own saved error), "put it back", tolerant spoken output phrases.
- Not built yet (found by audit): secure local device discovery (mDNS / DNS-SD; no code exists), parallel-plan blackboard use, Skill Studio page, Android-side notification categories beyond forwarding. Everything needing Windows, the phone or the GPU is still unverified here.


## Round 61 (device mesh and the rest of Stage 4-6 that could be built here)
- Built and unit-tested: `remote/discovery.py` (mDNS announce/scan, hostile-packet safe, never trusts), `core/device_mesh.py` + tool `device_mesh`, setting `LOCAL_DISCOVERY` (off), strict-offline allows link-local multicast, `core/notification_intel.py` wired into phone notifications + `notifications digest`, `automation/skill_card.py` + tool `skill_card`, parallel-plan blackboard, modes deep work / study / travel / sleep.
- NOT built: phone-side discovery (Android NsdManager, no JDK), a visual Skill Studio page beyond the existing Workflow Studio, VS Code / Office adapters, Windows UI Automation beyond what exists, the fuller security-dashboard panels, retention controls UI. Real-network discovery and everything on Windows / the phone remains unverified.


## Round 62 (verified start + cyber guardian)
- Built and unit-tested: tool `dev_session` (start / status / logs / stop; verified-up only when a port answers; crash causes from the log), `CommandExecutor.start_captured / exit_code / stop`, tool `guardian` + `core/guardian.py` (file bursts, startup changes, network listeners / destinations, backup checks, incident store with audit + notifications, upkeep hook, off by default).
- Unverified: both on your real PC (registry, connections, I/O counters, your projects). Not built: VS Code UI adapter beyond the existing open / edit / check tools, a guardian panel in the command center, per-process attribution of file changes (Windows needs auditing for that).

## Round 63 (coordination)
- Built and unit-tested (`tests/test_round63.py`, 25 tests): cross-device transactions (+ tool `transaction`), event intelligence (+ `situations`, event `BuildFailed`), attention firewall and mode-driven scheduling (+ `attention`; modes quiet / power saving / offline; gaming / presentation / meeting now hold work), what-changed-while-away (+ `what_changed`), dependency-aware health (+ `service_impact`). See `docs/round63-coordination.md`.
- Unverified: all of it on your real PC / phone; the Windows full-screen reader; real folder scans on large trees.
- Not built: a real meeting detector, git / history readers for the event diagnosis, a command-center panel for transactions and situations, Android NSD discovery (no JDK here).

## Round 64 (integration)
- Built and unit-tested (`tests/test_round64.py`): git context for event diagnosis, meeting detector, phone-verified transactions (auto-reconcile), guardian "open files" attribution, retention controls, app profiles for UI Automation, `vscode` + `office_info` tools, four command-center pages (Coordination, Guardian, Skills, Data kept), phone `overview` command. See `docs/round64-integration.md`.
- Unverified: all of it on real hardware; the Qt pages were never rendered. Not built: Android screens, a drag-and-drop Skill Studio, per-file change auditing.

## Round 65 (privacy + guardian)
- Built and unit-tested (`tests/test_round65.py`): privacy zones (+ enforcement in tools, memory, clipboard, phone remote), guardian USB-drive and file-integrity detectors, adaptive interaction advice. See `docs/round65-privacy-and-guardian.md`.
- Unverified on hardware: foreground reader, USB / volume-serial reader, file hashing on your files. Not built: Android sensors / camera, eye-gaze, embedded inference runtime evaluation.

## Fix: spoken "yes" / "confirm" not accepted (Nov 2)
- Cause (from `data/logs/jarvis.log`): during the shutdown question the recognizer heard short replies but dropped them (`stt_filler_dropped`: "yeah / ok / right" are on the noise-filler list), the strict yes-grammar rejected natural phrasing ("I confirm"), and a miss was silent. The 60 s timeout also raised an unhandled `CancelledError` in `main_window._on_confirmation`.
- Fixed: answer mode in the recognizer (no filler / short-text drop, nudged to yes / no / confirm / cancel), tolerant `classify_answer` (any "no" wins; chatter is not an answer), the answer also works through the normal reply path (`resolve_pending`), a visible "I heard ..." note when it is not understood, the timeout crash. `tests/test_confirm_answers.py` (53 cases). Not verified with your real microphone.

## Round 66 - learning quality control
Research (2025-26: arXiv 2605.12978 blind consolidation can fall below no-memory; 2505.16067 wrong experience propagates, selective add/delete helps; 2604.00131 forgetting as decay) showed the gap is not *more* learning but *trustworthy* learning. Existing stack (growth, self_improve, experience, replay) unchanged. Added `app/core/learning_quality.py` + tool `learning_audit`: Ebbinghaus-style strength, early error-propagation guard (shown-for requests failing >= baseline+25%), conflict detection (better record wins), corrected mistakes as hard test cases, reversible cleanup with a ledger (only flips `retired`; request history untouched). Rule-based, no AI call. Tests: `tests/test_round66.py`. Unverified: behaviour over weeks of real use; thresholds are reasoned defaults, not tuned.

## Round 67 - the learning loop acts on the quality layer
`Growth._reflect` (failed tool call -> same tool worked next turn with other arguments -> lesson, from the logs only); `relevant()` ranks faded items lower (never drops); `cases()` starts with corrected mistakes; upkeep runs `learning_quality.scheduled` weekly (logs `learning_audit_needs_review`; retiring still needs your OK via `learning_audit cleanup`); `SemanticIndex.add_exchange` obeys the `index` privacy-zone kind. Skill composition was NOT added: the Experience Compiler already turns repeated tool sequences into workflows. Not buildable here: Android screens (no JDK), drag-and-drop Skill Studio (needs a rendered Qt UI), real-device validation, embedded llama.cpp benchmark.

## Round 68 - "JARVIS 4.0" prompt audit + gaps
See docs/round68-sensors.md. Built: `core/mqtt_lite.py` (read-only MQTT 3.1.1, local broker, anonymous), `core/sensors.py` (stale/unknown/invalid honesty), tool `sensors`, `core/plugin_check.py` + tool `plugin_check`, trust writes detected `capabilities`. Tests `tests/test_round68.py`. Unverified on real sensors.

## Round 69 - local read-only MCP server
`app/mcp/server.py`, `--mcp-server-token` / `--mcp-server-revoke`, settings MCP_SERVER_ENABLED / PORT / TOOLS, `tests/test_mcp_server.py`, docs/round69-mcp-server.md. mDNS announcing already existed (remote/discovery.py). Android NSD + screens still need a JDK.

## Round 70
Sensor rules (`core/sensor_rules.py`, upkeep hook), sensors in twin + radar, weighted hard cases (`self_improve.HARD_WEIGHT`), plugin trust via restricted child probe (`core/plugin_sandbox.py`, `plugin_probe_child.py`), Learning panel spec. Tests `tests/test_round70.py`. Unverified: real sensors, Qt panel rendering, Windows behaviour of the probe (no memory cap there). Not built: per-file attribution.

## Round 71
See docs/round71-resources-and-verification.md. Verification states in the executor + `verification_coverage`; `core/resources.py` + `resources` tool (GPU fit with margin, model book, scheduler P0-P4, not yet wired into inference); Job Object limits on the plugin probe. Tests `tests/test_round71.py`.

## Round 72
Scheduler wired into index_documents (P2), tune_ai (P3) and the upkeep mode hold; `investigate_problem`; `deadline_risk`. Tests `tests/test_round72.py`. Unverified on real use.

## Round 73
Android 1.5.0 (UNCOMPILED - build with `packaging\build_android.ps1` and send any compile errors): Command Center screens (overview / find devices / sensors), NSD discovery, opt-in phone context (network, headset, orientation, still/moving, light - no location), "JARVIS stop" and "Wake PC" quick-settings tiles. PC side: `core/phone_context.py` (validated vocabulary, 15 min freshness, stale = unknown), `PhoneLink.collect` stores it, read-only `phone_context` tool. Tests `tests/test_round73.py` (fakes only; sensors/tiles unverified on a real phone).

## Round 74
Audit of the "Next-Generation" prompt (docs/round74-diagnosis-gateway.md). Built: `core/root_cause.py` (+ `root_cause` tool), `core/inference_gateway.py` (+ `inference_queue` tool; chat/voice not routed through it yet), `core/vault.py` (+ `vault_scan`; not yet wired into the index). Tests `tests/test_round74.py`. Fakes only; unverified on real hardware.

## Round 75
Phase 0 audit + new capabilities (docs/round75-new-capabilities.md): Foundry Local provider, geofence core + phone-event hook, A2A v1.0 client (off by default), sound-event monitoring, media index (migration 42, FFmpeg), photo analysis, context facts + event names. Not built: Android capture/geofence code, AppFunctions (Gradle/Kotlin-only, experimental), A2A server, speaker separation, new Qt screens. Tests `tests/test_round75.py`. Fakes only; unverified on real hardware.

## Round 76
The leftovers (docs/round76-leftovers.md): Android place alerts + take-a-photo (JARVIS Mobile 1.6.0), speaker turns, optional YOLO object detector, JARVIS as an A2A server (off by default, skill calls only), six new command-center panels. All Android sources now compile against the stand-in API (JDK in the test run); AppFunctions deliberately not built (needs Kotlin/Gradle). Unverified: anything on a real phone, real recordings, a real detector model, third-party A2A agents.

## Round 77
Daily life (docs/round77-daily-life.md): IMAP mail reader for any provider (read-only, BODY.PEEK), promises ledger (migration 43, `commitments`), `needs_me` ranked list, `environment_report` (AQI / UV / rain / daylight), `pc_checkup` (protection, updates, disks, battery wear, free space), Daily panel, two briefing lines. `.env.example` removed: `.env` is generated from the settings (`python run.py --write-env`). Unverified: a real mailbox, the PowerShell scripts on Windows, the live air-quality answer.

## Round 78
Audit of the "JARVIS 4.0 master" prompt against the repo (rounds 52-77 already cover nearly every section; no new pipeline was added). The one genuine gap was the schedule what-if simulator: `core/schedule_sim.py` + tool `schedule_whatif` (read-only; earliest-deadline-first over your working hours; postpone / drop / add task / protected daily block; baseline vs scenario; assumptions and a confidence label capped at "medium"). Effort comes from the task text "(2h)", your estimates, or a flagged 60 min guess; calendar time is not counted unless known. Tests `tests/test_round78.py`. Not built: ApplicationAdapter class layer (`app_profiles` + `press_control` already cover it), unified routing of chat/voice through the inference gateway (still pending, risky without a real-hardware test).

Round 78 (second half): `core/app_adapters.py` + read-only tool `app_adapter` - the ApplicationAdapter interface (detect / inspect / get_available_actions / execute_action / verify_action / recover_from_error) over `app_profiles`; route order structured tool -> UIA control -> shortcut offered; execution and verification injected, no verifier = reported "unverified", never success; risky save dialogs never auto-answered. `Gateway.observe` + `AIManager.chat/stream_chat` now run every live model call through the inference gateway as a tracked (not queued, not gated) request: id, lifecycle, latency, visible in `inference_queue`. Background work still uses the queue. Unverified on real Windows / real model.

## Round 79
"JARVIS 5.0" prompt audit (docs/round79-governance-and-learning.md). Built the four genuine gaps: mission capability leases (`core/leases.py`, tool `lease`, enforced in the executor after the permission gate - restrict only, fail closed), prediction ledger with calibration (`core/predictions.py`, tool `predictions`), technical experience memory with applicability check (`core/tech_experience.py`, tool `tech_experience`), evidence-backed `opportunities`. Migration 44; panel "Trust & learning"; tests `tests/test_round79.py`. Not built: OpenTelemetry export, third-party API discovery, automatic lease-per-mission, automatic prediction-before-action. Unit tests with fakes only.

## Round 80
The remaining JARVIS 5.0 items (docs/round80-remaining.md): automatic lease per mission (migration 45, `MISSION_AUTO_LEASE`), prediction before / after every mission step, experience candidates mined from the action log (`tech_experience suggest`), idle-time work (`core/idle_work.py`, `idle_work` tool, `IDLE_WORK_ENABLED`, briefing line), OpenTelemetry file export (`trace_export`), `discover_integration`. Bug fixed: `Upkeep.run_once` used `attention` without importing it (mode hold never applied; sensor-rule line raised). Research candidates (Graphiti, UI-TARS, Cua, Fara-7B) assessed from documentation only - none adopted, no proof of concept. Tests in `tests/test_round79.py`. Unverified in the live app.

## Round 81
Last open items (docs/round80-remaining.md, second part): per-step lease focus (migration 46), predictions around every changing tool call in the executor, idle model speed check (Ollama) + duplicate-document count, `find_app_api` web leads. Not built by decision: unattended model-driven memory consolidation. OpenGCUA: paper only, no code / licence found. Tests in `tests/test_round79.py` (28). Unverified in the live app.

## Round 82
Review-first memory merge (`core/memory_consolidation.py`, tool `memory_merge`, idle proposal job; model wording checked for invented facts, apply/undo, never unattended) and the screen-grounding trial (`python run.py --ground-trial MODEL`, UI Automation rectangles as ground truth, clicks nothing, downloads nothing). Tests in `tests/test_round79.py` (33). The trial itself and the Ollama wording path are unverified on real hardware.

## Round 83
Sending mail over SMTP (`core/mail_smtp.py`; `send_email` uses the .env mailbox when IMAP_USER / IMAP_PASSWORD are set, else Outlook; still DANGEROUS level with the full message shown; TLS only; `SMTP_HOST` / `SMTP_PORT` optional). Promises ledger now also scans WhatsApp unread previews and phone message notifications for requests made to you (`daily_tools.message_items / scan_messages`); a mailbox that cannot be read no longer aborts the scan. Lint clean in `app/core` (semicolon lines split, unused loop variable renamed). Tests `tests/test_round83.py`. Unverified against a real mail server / real WhatsApp. Still not built: AppFunctions (needs Kotlin + Gradle), Recall-style screen history and recording your own keyboard / mouse for teach-by-demonstration (privacy decisions that are yours), per-file attribution in the guardian (needs kernel file tracing), OpenGCUA (no public code).

## Round 84
The two opt-in recorders (docs/round84-opt-in-capture.md): `screen_history` (text of the window in front, never pictures; SCREEN_HISTORY_ENABLED + start; privacy zones and password fields skipped; retention; search / delete; migration 47) and `demo_record` (named controls you click during a session you start; no keyboard hook; draft -> save as workflow). Panel "Screen history". Tests `tests/test_round84.py`. Unverified on Windows. Still not built: AppFunctions (Kotlin + Gradle), per-file attribution in the guardian (kernel file tracing), OpenGCUA (no public code).

## Round 85
Audit of the "JARVIS 4.0 autonomous OS" prompt: nearly every subsystem already exists (see the tables in docs/round79-governance-and-learning.md and docs/round80-remaining.md). Built the gaps this prompt names that were missing: follow-up references (`core/referents.py` - "the first one", "number 3", "the last one", Bangla forms - resolved against the list a tool last returned, 10 min, never guessed; captured in the executor, hinted in the conversation context), four more bounded agents (`guardian` security reviewer, `monitor`, `phone`, `automator` - read-only except `phone`, none can overrule the permission system), notification history search with category filter and a follow-up command per category. Tests `tests/test_round85.py`. Not changed: mission state names (existing planned / running / verifying / paused / waiting / completed / failed / cancelled cover the listed states), no circuit breaker beyond the model health one, no separate model per agent (agents share the chat model by design). Unverified in the live app.

## Round 86
Circuit breakers for network tools (`core/circuit.py`, executor hooks, tool `service_breakers`: 3 transient failures in a row pause a service 60 s doubling to 15 min, one trial after, your own command is always tried, wrong requests never count). Per-file attribution from Windows' own records (`core/file_attribution.py`, tool `file_attribution`: reads Security event 4663 / Sysmon 11, 23, 26 through two fixed read-only PowerShell scripts; `setup` prints the admin commands; a source that is off or needs admin is said, never shown as "nothing happened"). Mission status in plain words (`taskstate.display_state`: awaiting approval / blocked / interrupted / ready; stored states unchanged). Tests `tests/test_round86.py`. Unverified on Windows (the event-log scripts need the recording switched on and, for the Security log, admin rights). Not built: AppFunctions (Kotlin + Gradle), OpenGCUA (no code), a model per agent (agents are permission scopes that share the chat model).
