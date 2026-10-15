# "Why did you do that?" and self-diagnostics

## Explainability (app/core/explain.py, tool `explain_action`)

Every request is a run, and every check the executor makes is written to the audit log with that run's id: the
risk level, whether Jarvis asked you and why, whether a plan was pre-approved, whether an agent contract refused a
tool, and whether the result was verified. `explain_action` turns the most recent run's trace into plain
sentences.

Ask **"why did you do that?"**, "why did you ask me?", "what did you just do?", or "explain your last action". You
can also point it at a topic ("why did you do that with my email"). It reports, per action: the tool, its risk,
why it asked or didn't, your answer, success or the reason it failed, and the after-the-fact verification.

It is honest about its limits: it explains the *safety decisions* because those are recorded facts, and does not
claim to replay the model's own reasoning for choosing the actions.

## Diagnostics & recovery (app/core/diagnostics.py, tools `diagnose_jarvis`, `repair_jarvis`)

**"diagnose yourself"** / **"check yourself"** (or Menu -> Diagnostics & recovery) runs read-only checks:

- database integrity (`PRAGMA quick_check`) and schema version,
- free disk space,
- requests or missions left "running" for over 30 minutes (a crash),
- errors in the last 24 hours and crash reports this week,
- whether the emergency controls are on,
- out-of-date Python cache files (the cause of a stale-bytecode test failure),
- the local AI (Ollama) answering, with models installed.

**"repair yourself"** fixes only Jarvis's own bookkeeping and reports exactly what it changed:

- close requests left running (marked interrupted),
- pause missions stuck mid-step (say "continue my mission" to resume),
- end idle phone sessions,
- delete out-of-date Python cache files (Python rebuilds them).

Repair never touches your files. It's MODERATE and asks first.
