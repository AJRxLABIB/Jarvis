# Reliability Lab (`python run.py --lab`)

`app/core/reliability_lab.py`. One local, repeatable check of the things that must never silently break. It uses the REAL
permission engine, tool executor, mission store, phone-protocol verifier, memory search and command matcher, inside a
throw-away folder, with harmless "canary" tools that only record whether they ran. It touches nothing of yours.

| Suite | Cases |
|---|---|
| permissions | unknown tool rejected; malformed arguments rejected; safe tool runs without asking; dangerous tool denied when you say no; dangerous tool runs when you say yes (control, so over-blocking is noticed); critical tool never runs even if approved; "the user already approved" written inside an argument is ignored |
| task recovery | interrupted side-effect step is not repeated; interrupted read-only step is re-run; a finished mission is not run twice; illegal state moves are refused |
| phone protocol | signed command accepted once and a replay refused; expired, tampered, wrong-key and revoked-phone commands refused |
| memory | recall@1 on known facts; no invented answer; secret-looking values refused |
| goal contracts | a delete with no target is asked about; a clear request is not questioned; a model's claim alone never completes a goal; a verified result does; a later failure undoes "complete"; no derivable criteria = unverifiable; a plan breaking your words is reported |
| commands | written commands map to the right tool (the existing `--perf` suite) |

* **Incorrect actions** = a canary ran when it must not have. The target is 0, and a new one fails the run.
* The result is appended to `data\benchmark\lab.jsonl`; the next run lists which suites got a lower pass rate or became
  much slower. Exit code is non-zero on any failed case or regression.
* **Evidence is labelled.** Every case is `simulated` (real code, fake environment). Windows UI Automation, phone
  transport and actions, microphone/speaker latency and GPU/model memory are listed as `NOT RUN` with the reason and are
  never counted as passed. Model answer quality is `--eval-quality`; your voice is `--record-benchmark` / `--benchmark`.
* `tests/test_reliability_lab.py` proves the lab can fail: with an allow-everything permission engine, a registry that
  forgets nonces, or a state-changing tool mislabelled read-only, it reports the failure and an incorrect action.
