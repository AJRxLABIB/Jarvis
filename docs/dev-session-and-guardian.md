# Verified project start and the cyber guardian (round 62)

## `dev_session` (tool) - `core/dev_session.py`, `tools/dev_session_tool.py`
`run_project` starts a project's usual command and reports "started". A started process is not a working server, so `dev_session start`:
1. passes through the SAME checks as `run_project` (the project's own command from its files, the command policy, the first-run trust question, emergency stop);
2. starts it with no window, output to a log file (`CommandExecutor.start_captured`, tracked with the other background commands so the emergency stop ends it);
3. waits (3-60 s) and reports one state: **running** (a port the log announced accepts a connection - the only "verified up"), running_with_errors, starting,
   unverified (alive but no listening port seen - never claimed as up), crashed (non-zero exit; cause read from the log) or finished;
4. explains known errors from a fixed table (missing Python / Node package, port in use, syntax error with file:line, program not installed, permission, database not
   reachable, npm error, unhandled exception) with the next step as text only.
`status`, `logs` (redacted tail) and `stop` (only what Jarvis started) work on that run. The log is untrusted data: it can only select one of the fixed explanations;
nothing is executed or edited because of it. PHP projects served by XAMPP's Apache are refused with a pointer to `run_project`.
Limits: readiness is judged from announced ports; a project that prints no port is reported "unverified". Needs the project's tools installed. Tested with a real
child process (log + stop) and fakes; not run against your projects.

## Cyber guardian (tool `guardian`) - `core/guardian.py`
Off by default (`CYBER_GUARDIAN=true`; scans every 3 minutes inside the existing upkeep loop; a manual `scan` always works). Names, sizes and times only.
* **file bursts** in `GUARDIAN_FOLDERS` (default Documents, Desktop, Pictures): many modified / deleted files, or many files with an extension the folders never had.
  The text says a sync, unzip or git checkout looks the same. Suspect programs come from disk-write counters and are labelled a guess.
* **startup entries** (Run / RunOnce keys, Startup folders) against a baseline; unusual locations (temp, downloads, encoded PowerShell) rank higher.
* **network**: a program newly listening on all interfaces; a program reaching many new internet addresses in one look.
* **backups**: none, stale (> 3 days) or a failed restore test.
The first scan only records what is normal. Each finding is an incident (evidence, confidence, what was NOT seen, suggestion) in the settings store, mirrored to the
audit log and the notification centre (security category). The same thing is not reported again until you review it (quiet for 6 h after dismissing).
`acknowledge` / `dismiss` / `contained` only mark the record. The guardian has no code path that stops a program, deletes or moves a file or changes a setting (a test
checks the source); any protective step is a normal tool that asks you first. Not antivirus: it sees changes and connections, not malware signatures.
Unverified here: the Windows readers (registry, psutil connections, I/O counters) and real folders; thresholds are starting points.
