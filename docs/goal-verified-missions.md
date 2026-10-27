# Goal-verified missions and skill reliability (round 47)

## Missions are checked against their goal
`app/core/missions.py::_check_goal` compiles the mission title with `goal_contract.compile` and evaluates it against the steps'
evidence when all steps are done:

| Verdict | Meaning | Result |
|---|---|---|
| complete | every required criterion verified | completed |
| incomplete | steps finished but a criterion has no supporting step | completed, note: "all steps finished, but I could not confirm: ..." |
| unverifiable | the title yields no checkable criteria | completed, note says only the steps were verified |
| contradicted | a step that should satisfy a criterion failed | **paused** ("the goal is not met") |
| error | the check itself failed | completed on the step results (logged) |

A heuristic never blocks a mission on a guess; only direct contradiction pauses. `start_mission` also refuses, before anything
runs, a plan that uses a tool the request forbids ("don't delete anything" + `delete_item`, "offline" + web tools).

## Skill reliability
`app/automation/reliability.py` + table `skill_runs` (migration 39, bounded to 50 rows per skill). After 3 failed runs in a row a
workflow/automation is switched off (not deleted) with the reason in its status and a notification; you re-enable it after
fixing it, which clears the streak. Not counted: stopped by emergency controls, declined by you, interrupted by a restart,
missed. `list_automations` shows "N of M recent runs succeeded".

Limits: the goal check is only as good as the heuristic criteria; reliability measures run outcomes, not whether the UI
changed; no parameter templating of skills yet.
