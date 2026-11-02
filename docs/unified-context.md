# Unified request context and agent blackboard (round 60, Stage 3)

## Why
Six systems each knew part of the picture (context engine: what you're doing; world model: projects / goals / people; memory; missions; device registry;
performance plan). Nothing merged them for one request, nothing labelled how each fact is known, and agents of one request could not see each other.

## `app/core/request_context.py`
`for_request(ctx, request)` -> `RequestContext`: items from the real systems, each with **evidence** (`verified` measured/read now, `retrieved` stored and
confirmed by you, `inferred` a signal-based guess or unconfirmed suggestion, `predicted` forecast, `unknown` looked for, not available), a source, a confidence,
and freshness (age against a lifetime: `stale` is flagged "may be out of date", very old items are dropped). Selection is by relevance to the request
(English, Bangla and Banglish words), under per-section budgets and a total budget (1800 characters): permissions / emergency switches, what you are doing
and a waiting approval are always included; everything else only when it relates to the request; private people only when named. Results are cached 20 s.
A failing source becomes a line under "not available", never an error.

`clarify(request, ctx)` returns ONE focused question when the request points at something nothing in the context can resolve ("that project", "continue my
previous task", "my phone"), or when two equally confident projects compete and the request names neither. It is given to the model as an instruction to ask
instead of guessing; it never blocks a command by itself.

Wired into `conversation._situation` (replaces the older world-only compile, which stays as the fallback). Tool `request_context` shows the evidence behind a
request ("what context are you using", "why did you assume that").

## `app/core/blackboard.py`
One in-memory board per request (run). Typed entries only (doing / result / need / claim with an evidence label). `begin` gates every orchestrated step:
already done (a state-changing call that succeeded earlier in the request is not repeated across delegations), busy (the same call is in flight), failed
before (same call failed twice with nothing changed), loop (delegation chain revisits an agent or is deeper than 4). Read-only calls may repeat.
`orchestrator.run_plan` uses it for sequential plans. The executor's own one-shot rule (`Run.completed`) still applies underneath.

## Limits (honest)
* Relevance is word overlap, not embeddings; unusual phrasing can miss an item.
* Parallel plans do not use the blackboard yet.
* The evidence label of a memory depends on its `provenance`; memories saved before provenance existed count as user-confirmed.
