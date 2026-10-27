# Time-travel task debugger (round 48)

Tool `task_replay`, rules in `app/core/replay.py`, panel Intelligence > "Task replay". Reads only what Jarvis already records
for a run (`agent_runs` + `audit_log.run_id`): no screenshots, audio, keystrokes or file contents are added.

| Action | What it does | Asks? |
|---|---|---|
| list | recent runs that failed or diverged | no |
| show | recorded timeline; each line tagged `[recorded]`, `[reconstructed]` or `[not recorded]` | no |
| diagnose | earliest divergence (refused / failed / unverified / no outcome), likely cause (from the error code, via `capability_gaps` + `retry` classification), minimal recovery | no |
| simulate | runs the REAL mission engine against a MOCK executor that answers with recorded results (throw-away DB; retries are exercised, sleeps skipped). Nothing external runs | no |
| live | re-runs ONE call through the real executor (permissions, emergency stop and validation re-checked now) | yes |
| hide / unhide | which tools' arguments and results are hidden in replays (defaults: type_text, send_email, send_message, read_file, read_document, read_whatsapp, clipboard, run_command, remember, journal, save_contact); unhide asks | unhide |

## Safety rules (tested)
* Live replay is refused unless the action is read-only/repeatable (`autonomy.is_read_only`, risk SAFE) AND its recorded arguments are
  intact (not hidden, redacted or missing). A deletion, message, purchase or device command is never replayed because it failed.
* Recorded arguments are re-redacted on output; hidden tools show `[hidden by your replay settings]`.
* Missing data is named ("no result was recorded", "arguments were not recorded", "duration not recorded") - never filled in.
* Not deterministic: model output, timing, network and app state are not reproduced; every live/simulated result says so.
* `task_replay` is in `PERSISTENT_TOOLS` (a tainted conversation asks first) and not readable-only for live/hide/unhide.

## Limits
Durations exist only for runs recorded after round 46 (`elapsed_ms`); the model's reasoning and your PC's state at the time are not
recorded; arguments of a hidden tool can't be replayed; simulation shows whether the engine's logic reproduces the recorded outcome,
not whether the world would. Controlled reproduction in a separate test environment (spec mode 3) is limited to read-only calls on
this PC. Tests: `tests/test_replay.py` (16), 3 Reliability-Lab cases.
