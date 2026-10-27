# Goal contracts (`app/core/goal_contract.py`, tool `goal_check`)

Turns an informal request into an explicit goal, and later compares what really happened with it. Deterministic: no model
call, no network, nothing stored.

**compile(request)** gives: the outcome; constraints you stated ("without installing anything", "don't delete", "offline",
"ask me first", "just look"); a deadline ("tomorrow", "this week" = that Friday, "by friday", "by 2026-12-01"); the project
(named, or assumed ONLY when exactly one is known - the assumption is listed); acceptance criteria (open X, reminder, to-do,
checks/tests, git status, email, delete, "ready for development" with an optional checks criterion); risks; conflicts with
what is already on your plate (a near-duplicate to-do/mission; a day with 3+ things already due; a date in the past); and
the questions that materially matter - a delete with no target (safety), which of several projects (correctness), when to
remind you (correctness), who an email goes to (privacy). A clear request is never questioned.

**check_plan(contract, tools)** reports a plan that breaks what you said (e.g. `install_software` after "without
installing"). What you say now always beats a remembered preference.

**evaluate(contract, evidence)** is the evidence ledger. Each criterion is verified / strongly supported / tentative /
unknown / contradicted. The newest direct observation decides; a user statement or document is "strongly supported" but not
verified; a model's guess is only "tentative" and can never complete a goal. No percentages are shown: there is no
calibration data to justify them. A request whose criteria cannot be derived is "unverifiable", never "complete".
`evidence_from_steps` turns mission steps into evidence (done = verified, failed = against, not run = silent).

**goal_check** (read-only tool): `goal_check request="..."` explains what it understood and what it would ask; with no request
it judges the latest mission's real step results against its goal.

Limits: criteria come from a fixed set of phrases (English only), so most free-form goals say "can't tell what would count as
done" - that is deliberate; it is not wired into the automatic mission flow yet (you ask for it); it does not call the model;
Bangla requests are not parsed. Tests: `tests/test_goal_contract.py`, and 7 cases in the Reliability Lab (`--lab`).
