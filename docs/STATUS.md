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
