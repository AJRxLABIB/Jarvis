# Anomaly detection and the Experience Compiler (round 46)

Both are local, deterministic (no model), read-only on your data, and unverified on real hardware.

## Anomaly detection - `app/core/anomaly.py`, tool `anomalies`, radar signals

"Unusual for you", not a fixed threshold. A recent window is compared with your own earlier history using the median and
MAD (robust z-score, limit 3.5, plus a minimum ratio so tiny wobble never flags):

| Check | Source | Flags when |
|---|---|---|
| tool latency | `audit_log` (`elapsed_ms`, recorded from this version on) | median of today's successful calls is far above the tool's usual, 3+ calls today, 8+ earlier |
| tool failures | `audit_log` | today's failure rate is implausible under the earlier rate (binomial tail < 1 %), 2+ failures |
| PC load | `metrics` | last hour's memory / CPU far above the past two weeks (and 15+ points higher) |
| spending | `expenses` | a recent expense far above that category's usual (2x+), 8+ earlier entries |

Honest limits: fewer than 8 earlier samples means *no verdict*, never a guess; the score says how unusual, not why; a flagged
value can be fine. Calls refused by you or by a safety rule are not counted as failures. Alerts go through the radar, so snooze,
mute and "not useful" (three mute for a week) apply; category is `system`. Panel: Intelligence > "Unusual for you".

## Experience Compiler - `app/core/experience.py`, tool `experience`

Finds routines you never announced: the same successful tool steps, in the same order (within 10 minutes of each other), at
least 3 times on at least 2 different days in the last 30 days, from Jarvis's own audit trail. Each proposal shows steps, counts,
last seen, which arguments changed between runs (the latest value is used and the change is stated) and a rough time estimate.

* Reads only what Jarvis itself did - never keyboard, mouse, screen, microphone or files.
* Excluded: tools automations may not use, dangerous/critical calls, failed calls, anything whose arguments held a secret.
  An excluded or failed call also breaks the sequence ("A, x, B" is not "A, B").
* Longer patterns absorb their sub-patterns; already-saved workflows and dismissed proposals are not offered again.
* `save` is MODERATE: it asks on screen, validates every step like any workflow, and stays out of missions, automations and
  tainted conversations (`experience` is in `FORBIDDEN_STEP_TOOLS`, `NOT_A_STEP`, `PERSISTENT_TOOLS`).
  Voice/phone/automation can not approve it. Panel: Intelligence > "Routines you repeat".

Not covered: calls made outside Jarvis; routines that vary in order; parameterised workflows (varying arguments are reported,
not templated); the time estimate is from past durations only.

Also changed: the audit record of every tool result now carries `elapsed_ms`.
Tests: `tests/test_anomaly_experience.py` (17), 6 new Reliability-Lab cases ("anomalies and routines").
