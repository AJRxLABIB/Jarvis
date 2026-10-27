# Personal intelligence (Intelligence Center: command center > Intelligence)

All of it is local, deterministic code over data already on this PC: no model call, no network, no telemetry. Every button
runs the normal tool (as your own command), so permissions, confirmations and the audit log apply exactly as in chat.

| Capability | Code | Tool | What it does / where it stops |
|---|---|---|---|
| Goal contract (earlier round) | `core/goal_contract.py` | `goal_check` | what a request means, what counts as done, material questions only, evidence-based completion |
| Decision records | `core/decisions.py` | `decision` | what was chosen over what, why, how it turned out; editable (a correction keeps the old value), deletable (asks). `relevant` shows earlier decisions with their age; nothing consults them automatically |
| Option comparison | same | `decision compare` | each number is labelled measured / simulated / estimated; an unknown is left out, never zero; no ranking without comparable numbers; <10% margin = "too close"; weak evidence + irreversible winner => "try the reversible option first" |
| Preferences | `core/preferences.py` | `preferences` | explicit statements are confirmed at once; observed corrections are only candidates, never applied, and after 3 sightings Jarvis may ASK. Profiles (development, study, research, planning, gaming, general) change style/tools/context only. Precedence: security > this task > profile > confirmed long-term (project beats global) > defaults. Statements that try to switch off approvals are refused. Learning can be switched off; any preference can be removed |
| Active information gathering | `core/gather.py` | `investigate` | hypotheses + cheap read-only probes; picks the probe that settles the most open causes per cost, never repeats one, skips private probes, stops at a supported cause / budget / nothing useful left, and lists the evidence still missing. Built-in: why the local model isn't answering (Ollama up? model installed? free RAM? recorded errors?) |
| Knowledge synthesis | `core/synthesis.py` | `research_brief` | a brief from memories, decisions, indexed documents and the graph, with sources, "possibly outdated" marks (180 days), contradictions side by side, unresolved items and next actions; says plainly it is offline. Gaps (README missing, a decision with no reason) and PROPOSED graph links (never added or treated as facts) |
| Long-horizon goals | `core/longgoals.py` | `goal_review` | progress = ticked tasks; pace from your own completions (<3 = no pace, no forecast); forecast is a labelled range; availability only caps pace; stalled after 14 days; replans are proposals and change nothing; weekly review (can be switched off) |
| Radar additions | `core/radar.py` (+ hook in `core/proactive.py`) | `radar_feedback` | new local signals: low RAM, low disk on the data drive, a trusted phone silent for 24 h. Snooze / mute / useful / not useful; three net "not useful" hold an alert back for a week (reversible). The existing engine still does scoring, quiet hours, rate limits and de-duplication |
| Capability discovery | `core/capability_gaps.py` | `capability_report` | recurring failures from the audit log classified by cause (missing tool / permission / information / unclear intent / model limit / platform / resource / unreliable) with a proposal each (approach, security, test plan, rollback, upper-bound benefit). A missing permission never produces "widen it". Inventory: risk, read-only, network, tests naming it (unknown when the tests folder isn't present), failure rate |

Data: migration 37 adds `decision_records`, `preferences` and `radar_feedback` (existing data untouched).

Limits (stated, not hidden): English phrases only; the briefs and gaps are keyword-based, not semantic; the investigation
covers one problem (the model) so far; preference observation has no automatic source yet - `observe()` exists, nothing calls
it on its own, so noticed patterns appear only when something is wired to record them; nothing is verified on your real
data or screen. Tests: `tests/test_intelligence.py` (32), 6 cases in the Reliability Lab.
