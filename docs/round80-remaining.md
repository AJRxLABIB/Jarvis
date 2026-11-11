# Round 80 - the remaining items from the JARVIS 5.0 audit

## Built

| Item | Where | Behaviour |
|---|---|---|
| Automatic lease per mission | `leases.ensure_auto`, `missions._auto_lease`, migration 45 (`auto` flag), `MISSION_AUTO_LEASE` | When a mission starts it gets a lease listing exactly the tools of its approved plan (network only if a step needs it, 24 h, calls = 6 per step, min 20). A lease you made is never replaced. An automatic lease that expired while the mission was paused is renewed from the same plan; a revoked one is not. Failure to create it never blocks the mission. The risk-level limit of automatic leases is "up to risky" - the tool list is what restricts. |
| Prediction before every mission step | `predictions.auto_predict / auto_resolve`, `missions._predict_step / _resolve_step` | Before a step: "this tool will succeed" at 85 %, shrunk by that tool's own record. After the step: resolved right / wrong with the step result as evidence. Retries count once. Kept to the newest 2000. |
| Experience candidates from the log | `tech_experience.load_audit / mine`, tool action `suggest` | A call that failed and then succeeded with other arguments within 15 minutes becomes a candidate. Secret-looking argument values are hidden. Nothing is saved without you. |
| Idle-time work | `core/idle_work.py`, `Upkeep._idle_work`, tool `idle_work`, `IDLE_WORK_ENABLED`, `IDLE_WORK_AFTER_MINUTES` | Runs from the existing upkeep loop only when: no input for N minutes, not on battery, CPU under 40 %, the mode allows background work. One job at a time, 20 s per pass, stops when you return. Jobs: prune predictions, prune settled promises, refresh the opportunity list (one line in the morning briefing, dropped after 18 h), look for experience candidates. Every run is logged with its real result. No model, GPU or network use. |
| OpenTelemetry export | `core/otel_export.py`, tool `trace_export` | One request -> OTLP/JSON file in the exports folder: a root span plus one span per tool call (`gen_ai.operation.name=execute_tool`, `gen_ai.tool.name`, risk level, decision, ok, error type, recorded duration). Arguments and request text left out by default; with them, secret-looking values are replaced; result messages never exported. Local file only. |
| Integration discovery | `core/integration_discovery.py`, tool `discover_integration` | For a program: existing Jarvis tool, command line on PATH, COM automation class, URL scheme, UI Automation - ranked, with what each can and cannot do. States what was not checked (vendor web API, licence, sign-in). Generates nothing. |

Also fixed while wiring idle work: `Upkeep.run_once` used `attention` without importing it. The scheduler mode hold therefore never applied (the error was swallowed), and with a config folder set the sensor-rule line raised, so transaction reconciliation and memory retention after it did not run in that pass.

## Research candidates - assessed from documentation only (no proof of concept was built)

Hardware in mind: RTX 4050, 6 GB VRAM, already running an 8B chat model.

| Candidate | What the sources say | Decision |
|---|---|---|
| Graphiti (temporal knowledge graph) | Apache-2.0. The embedded Kuzu backend is deprecated (upstream unmaintained); the project now points to Neo4j or FalkorDB (an embedded FalkorDB variant needs Python 3.12+). It extracts entities with LLM calls on every episode. | Not adopted. It would add a graph database and constant model calls on a 6 GB card. Jarvis keeps its SQLite graph with provenance; worth revisiting if a light embedded backend settles. |
| UI-TARS 1.5 7B / UI-TARS Desktop | Apache-2.0 model and app. Third-party guides estimate ~16 GB for full precision and 4-8 GB quantized (estimates, not measurements). Windows support claims conflict between sources. | Not adopted now. A quantized 7B vision model cannot share 6 GB with the chat model; it would need model swapping. A proof of concept (quantized, as an optional vision model for `click_thing` only) is the sensible next experiment. |
| Cua | Infrastructure (sandboxes, SDKs, benchmarks) for computer-use agents; its docs suggest 16 GB+ VRAM for local 7B models. Licence not confirmed in my search. | Not adopted. Jarvis already has its own executor, verification and UI Automation layer. |
| Fara-7B | Listed as MIT on Hugging Face; base Qwen2.5-VL-7B; launch coverage ties it to Microsoft's Magentic-UI research platform; third-party figures of 7-8 GB at 4-bit are unverified. | Not adopted. It does not fit 6 GB beside the chat model, and it is described as not production-ready. |
| OpenGCUA, long-horizon benchmarks | Not researched this round. | Open. |

Sources: PyPI `graphiti-core` 0.29.3 page; github.com/getzep/graphiti; cua.ai docs (VLMs); UI-TARS Desktop repositories and guides; huggingface.co/microsoft/Fara-7B; Microsoft Research blog on Fara-7B; SiliconANGLE and VentureBeat coverage. One search per candidate - not exhaustive.

## Still not built

* A lease is not narrowed per step (the whole plan's tools are allowed for the whole mission).
* Predictions are automatic for mission steps only, not for single tool calls in a chat.
* Idle work does not run the AI model (no memory consolidation by the model, no model benchmarking while idle).
* Integration discovery does not search the web for vendor APIs.
* No live OTLP network exporter (file export only, by design).
* Everything here is covered by unit tests with fakes; none of it has run inside the live app on Windows (idle detection, the registry checks and the upkeep hook in particular).

# Round 81 - the last open items

| Item | Where | Behaviour |
|---|---|---|
| Lease narrowed per step | `LeaseStore.focus`, `missions._focus_step`, migration 46 | While step N of a mission runs, its automatic lease allows only step N's tool. Leases you made yourself are not narrowed. |
| Predictions for single calls | `ToolExecutor._predict / _resolve_prediction` | Every call that changes something (risk level 1+) or can verify itself is predicted before it runs and resolved with its real result. Read-only calls are skipped; mission steps are still predicted by the mission runner (no double counting). |
| Idle work with the model | `idle_work.speed_job` | With Ollama: one tiny daily request to the chat model, tokens/s kept (30 readings). A reading under 60 % of your own median is reported as a slowdown. |
| Idle duplicate check | `idle_work._memory_duplicates` | Counts near-duplicate indexed documents for review; merges nothing. |
| Web leads for vendor APIs | tool `find_app_api` (web category, needs network) | Runs one web search and returns the links as unverified leads, with the untrusted-content note. |

Deliberately not built: model-driven memory consolidation while idle. Letting the model merge or rewrite memories unattended can make memory worse than leaving it alone (see round 66); duplicates are reported for your review instead.

OpenGCUA: one search found the paper (arXiv 2610.22200, a harness that has agents write Python programs instead of clicking; one third-party summary credits Alibaba) but no code repository or licence. Nothing to evaluate yet.

UI-TARS swap-in trial: not done - it needs your GPU and a model download you would have to approve.

# Round 82 - memory merge (review-first) and the grounding trial

* **Memory consolidation, review-first** - `core/memory_consolidation.py`, tool `memory_merge`, idle job "propose memory merges" (every 2 days, up to 3).
  Pairs of near-identical memories in the same category are PROPOSED; private, disabled, superseded, expiring and event memories are never touched.
  With Ollama the local model words the merge and the wording is rejected if it adds a number, a name, new words, is too long or drops too much - then the
  rule "keep the longer one" is proposed instead. `apply` works only if both memories still read as proposed (else "stale"); the kept memory's old wording
  stays in its revision history, the other is switched off (not deleted); `undo` restores both; a rejected pair is never proposed again.
  Limit: similarity is word overlap, so two memories that mean the same in different words are not found, and two similar sentences about different
  things can be proposed - which is why nothing is applied without you.
* **Screen-grounding trial** - `app/validation/grounding_trial.py`, `python run.py --ground-trial MODEL`. Uses a vision model that is already installed
  (it downloads nothing): the foreground window's named controls from UI Automation are the ground truth, the model is asked where each is, a point inside
  the control's rectangle is a hit. Reports hits, replies without usable coordinates, median time. Clicks nothing. This is the harness for the UI-TARS
  trial: install a quantized UI-TARS (or any vision model) in Ollama yourself, then run it. Not run here (needs Windows, a GPU and a model).
* Lint: 6 unused imports removed; 8 older style warnings remain (semicolon lines in `interaction.py`, two unused loop variables in `guardian.py`).
