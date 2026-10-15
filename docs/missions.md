# Missions 2.0 and the background task center

Code: `app/core/missions.py`, `app/tools/goals_missions.py`, `app/core/task_center.py`, `app/ui/task_center.py`.

## Plans

A mission is a list of tool calls (at most `MAX_STEPS`). Each step may also have:

| field | meaning |
|---|---|
| `depends_on` | numbers of **earlier** steps that must finish first. A forward or self reference is rejected. If a needed step was skipped, this step is skipped too (with the reason) instead of running on missing input. |
| `milestone` | a group name ("Research", "Slides"). "How is my mission going" shows progress per milestone. |
| `checkpoint` | pause before this step. The mission waits (task center: WAITING FOR APPROVAL) until you say "continue my mission"; that request is confirmed like any resume. |

The whole plan is still validated and simulated first and you approve it once; dangerous steps still ask one by one;
missions, rollbacks, automations and settings changes can never be steps (`NOT_A_STEP`).

## Rollback

"Roll back the mission" (tool `rollback_mission`, level 2 - always asks) takes the most recent mission with finished
steps and undoes them newest first. Each step's exact inverse was recorded when it ran (`app/core/undo.inverse_of`:
moves, renames, created files, code edits, volume/brightness, toggles...). Each undo runs through the executor, with its
own checks and audit entry. Steps with no exact inverse are listed as "can't be undone" and stay done. If an undo fails
the mission is left paused with "rollback incomplete" and the failure is reported.

States: planned, running, paused, completed, failed, cancelled, rolled_back (steps: pending, running, waiting,
approved, done, failed, skipped, undone). Database migration 18 adds `milestone`, `checkpoint` and `inverse` to
`mission_steps` (forward only, backup first).

## Background task center

One read-only view over the local database:

| source | status mapping |
|---|---|
| missions | planned QUEUED, running RUNNING, paused at a checkpoint WAITING_FOR_APPROVAL, paused PAUSED, completed, failed, cancelled, rolled back |
| scheduled automations (saved workflows are not background work) | enabled QUEUED (with next run and last result), disabled PAUSED |
| self-improvement proposals | proposed QUEUED, testing RUNNING, tested WAITING_FOR_APPROVAL, failed, applied COMPLETED, rejected CANCELLED, rolled back |
| requests still running (other than the one asking) | RUNNING |

Active tasks come first. A source that can't be read (an old database) is left out, never guessed. The dialog's buttons
send ordinary requests ("continue my mission", "cancel the mission", "roll back the mission", "test proposal 3"), so
every one goes through the usual permission and confirmation checks - the task center itself changes nothing.

Commands: "show background tasks", "open the task center", "what's running in the background", "roll back the
mission", "undo my last mission".
