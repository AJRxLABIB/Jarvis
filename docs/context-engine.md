# Context Engine (3.16)

`app/core/context_engine.py` - one structured snapshot of what you're doing, with a source and a certainty for
every item. Used by: the conversation (a one-line "Right now: ..." in the model's prompt), the developer tools
("run the tests" = the project you're editing), the `current_context` tool ("what am I working on"), and the
context panel (Menu -> What Jarvis sees).

## What it reads (only what Jarvis may already see)

| Item | From | Certainty |
|---|---|---|
| active_app, active_window | the foreground window's title and process (never the screen) | known |
| project, workspace | the editor's title matched to a project you told Jarvis about (DEV_PROJECT_DIR, "my project folder is ...", trusted projects) | likely |
| current_file, unsaved_changes | the editor's title | likely |
| browser_page | the browser window's title (no URLs, no page content) | known |
| git_branch, git_changes | `git status --short --branch` in that project (fixed arguments, cached 30 s) | known |
| current_goal, current_mission | goals and Mission Control | known |
| pending_approval | the approval waiting in the window | known |
| recent_actions | the audit log (what Jarvis did, last 3 hours) | known |
| unread_notifications | the notification center | known |
| device_state | battery, CPU, RAM (psutil), online (network check) | known |
| jarvis_mode, emergency | modes and emergency switches | known |

Anything not available is listed as *unknown* with the reason ("window titles are off", "not a git repository").
`confidence` is coverage (how many core items are known/likely), not a claim of truth.

## Privacy and retention

* Window titles are read only when `CONTEXT_WINDOW_TITLES=true` (default) and the mode allows capture - Private
  and Presentation modes turn them off. Secrets in titles are redacted.
* Snapshots live **only in memory**: the last 30, at most 15 minutes. Nothing is written to the database.
* `current_context` is a *private* tool: after it runs, actions that send data out ask first (taint).

## Snapshot example

```json
{"timestamp": "2026-09-26T21:40:00+06:00", "confidence": 0.77, "active_app": "VS Code",
 "active_window": "auth.js - TMS - Visual Studio Code", "project": "TMS", "workspace": "D:\\TMS",
 "current_file": "auth.js", "git_branch": "develop", "git_changes": 3,
 "current_goal": "Finish the TMS login (40%, due 2026-10-01)", "jarvis_mode": "coding",
 "device_state": {"battery": 78, "charging": true, "cpu": 18, "ram": 42, "online": true}}
```
