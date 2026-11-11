# Round 79 - "JARVIS 5.0" prompt: audit and the four genuine gaps

Method: every one of the 26 signature features was checked against the code, not the module names. Most already exist; no second engine was added.

| Prompt feature | Already in the repo | Built now |
|---|---|---|
| 1 Life OS / domains | workspaces, privacy zones, projects | - |
| 2 Digital twin, what-if | `core/twin.py`, `schedule_sim`, resources | - |
| 3 Predictive intent | proactive, routines, radar, experience compiler | - |
| 4 Mission control | missions (checkpoints, restart, verify), goals, longgoals | - |
| 5 Sleep-time intelligence | upkeep, scheduler modes, learning audit, briefing | not a separate idle engine |
| 6 Experience compiler | `core/experience.py`, foundry, skill reliability | - |
| 7/18/26 Simulation lab, workshop, evolution sandbox | `app/evolution` (lab, proposals, deploy), worktree, plugin sandbox | - |
| 8 Agent swarm | agents, blackboard, parallel, agent contracts, A2A | - |
| 9 Computer intelligence | UIA, screen_verify, app_profiles, app_adapters | - |
| 10 API builder | foundry, plugin_check, plugin sandbox | discovery of third-party APIs not built |
| 12 Memory | memory2, knowledge graph, decisions, learning quality | **technical experience records** |
| 13 Model intelligence | router, resources, gateway, model benchmark | - |
| 17 Opportunity discovery | radar, capability_gaps (separate lists) | **`opportunities`** (one ranked, evidence-backed list) |
| 19 Agent governance | permissions, autonomy, agent contracts, taint, audit chain | **mission capability leases** |
| 22 Benchmark | reliability lab, selfbench, multilingual eval, model benchmark | not renamed "JIB" |
| 23 Observability | trace_request, replay timeline, audit chain | no OpenTelemetry export |
| 24/25 World model, hypothesis lab | world, investigate (hypotheses), root_cause | **prediction ledger** (predict -> outcome -> calibration) |

## What was built

* `core/leases.py` + tool `lease` + executor hook (`ToolExecutor._lease_denies`): a mission with a lease may call only the listed tools, up to a risk level,
  inside a folder, with or without network, for a time and a call budget. Checked AFTER the permission gate, so it can only restrict. Expired / used-up /
  revoked leases block the mission (no fall-back to "no lease"). A failing lease check fails closed. Denials are audited (`lease_denied`).
  Missions without a lease are unchanged.
* `core/predictions.py` + tool `predictions`: write a prediction with a confidence before acting, resolve it with evidence (required), report calibration
  (hit rate per confidence band, Brier score, over/under-confident). New confidences on a topic are shrunk toward the measured hit rate, never raised.
  Under 5 resolved predictions it says "not enough history".
* `core/tech_experience.py` + tool `tech_experience`: problem, error, environment, FAILED attempts, fix, verification. Recall compares the environment:
  "applies" / "may not apply (what differs)" / "unverified fix" / old. Nothing is executed.
* `core/opportunities.py` + tool `opportunities`: repeated workflows, failing tools, paused missions, goals with no tasks, overdue promises, overconfident
  predictions - each with evidence, benefit, complexity, risk, next action; at most 7; dismissals persist.
* Migration 44 (`capability_leases`, `predictions`, `tech_experiences`), command-center panel "Trust & learning", tests `tests/test_round79.py`.

## Honest limits

* Leases are not created automatically when a mission starts; you grant one with the `lease` tool. Folder scope only sees arguments named like a path.
* Predictions are recorded when the model (or you) calls the tool; Jarvis does not yet predict before every action by itself.
* Technical experiences are matched by wording, not meaning; they are written when asked, not mined from logs.
* Not built: OpenTelemetry export, automatic discovery of third-party APIs, a separate idle-time engine, Graphiti / UI-TARS / Cua integrations (research
  candidates only - none was evaluated with a proof of concept this round).
* Verified with unit tests and fakes only; not run inside the live app.
