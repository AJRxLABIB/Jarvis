# Model router 2.0 (3.19)

`app/ai/router.py` decides the role, `app/ai/manager.py` picks the installed model and falls back.

| Role | When | Model |
|---|---|---|
| fast | short everyday requests | AI_FAST_MODEL -> AI_MODEL |
| normal | everything else | AI_MODEL |
| smart | explanations, writing, analysis, plans, multi-step tasks | AI_SMART_MODEL -> AI_MODEL |
| coding | code, errors, stack traces, failing tests/builds, languages and frameworks; "fix it" / "what's wrong here" while you're in an editor (context engine) | AI_CODING_MODEL -> AI_SMART_MODEL -> AI_MODEL |
| vision | screens and images (vision tools) | VISION_MODEL -> a vision-capable installed model |

* A role model that isn't installed is skipped (logged).
* A role model that fails at request time (crash, timeout, removed) falls back **once** to AI_MODEL; if AI_MODEL
  itself fails the error is reported - never hidden. Streaming falls back only before the first word.
* **Private requests** (Private mode, or words like password, bank, medical, confidential) are only sent to an
  Ollama on this PC or your own network (LAN, Tailscale). An Ollama host on the internet is refused.
* "Which model are you using?" (`model_status`): every role, whether it's installed, where the models run, and
  which model answered the last request (with any fallback).

Configuration: `AI_MODEL`, `AI_FAST_MODEL`, `AI_SMART_MODEL`, `AI_CODING_MODEL` (new; e.g. `qwen2.5-coder:7b` -
`ollama pull qwen2.5-coder:7b`), `VISION_MODEL`; Settings -> AI.
