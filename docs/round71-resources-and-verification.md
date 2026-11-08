# Round 71 - audit of the "next generation" master prompt, verification states, resource orchestration

## Audit (ten initiatives vs the repo)
| # | Initiative | State before this round | Done now |
|---|---|---|---|
| 1 | GPU / AI resource orchestrator | perf modes, `fit_check`, nvidia-smi snapshot, Ollama load/unload/`/api/ps`, gaming/meeting modes | **built:** NVML-or-smi GPU read, measured-memory fit plan with a VRAM margin, loaded-model book, idle-unload rule, P0-P4 workload scheduler, `resources` tool (`core/resources.py`) |
| 2 | Visual intelligence | OCR, screen read, UIA elements, screen verify, vision model hook | not changed (needs Qwen3-VL benchmarks on your GPU) |
| 3 | Retrieval | hybrid search, dedupe, memory conflicts, Bangla set | not changed (embedding / reranker choice needs your models) |
| 4 | Android command center | pairing, signed commands, overview, WoL | not changed (needs a JDK) |
| 5 | Diagnostic engine | troubleshoot, event intelligence, health history | not changed |
| 6 | Plugin isolation | sha-pinned manifests, capability scan, probe child | **hardened:** Windows Job Object (512 MB, one process, kill-on-close) on the probe |
| 7 | Android sensors | none | not changed (needs a JDK) |
| 8 | Dev agent | import graph, impact analysis, worktree, code review | not changed |
| 9 | Personal intelligence | goals pace / replan / weekly review, day plan | not changed |
| 10 | Action verification | action lifecycle, per-tool `verify`, UNCERTAIN end state | **built:** every success carries `verification` = VERIFIED / FAILED / UNCERTAIN / NOT_SUPPORTED / NOT_REQUIRED; tool `verification_coverage` lists tools that change things but cannot prove it |

## Verification states
`NOT_REQUIRED` = a read-only SAFE tool. `VERIFIED` = the tool's own check read the real world afterwards and found it. `FAILED` = it looked and it was not so (the result becomes an error). `UNCERTAIN` = the check could not run (the tool's word only). `NOT_SUPPORTED` = the tool changes things and has no check: "done" is the tool's word, not evidence.

## Resources
`resources` (status / models / fit). The scheduler is a library: nothing in conversation, voice or indexing runs through it yet, so today it only has tests. Wiring it in (P1 voice, P3 indexing, hold while gaming) is the next step. NVML is optional (`pip install nvidia-ml-py` gives `pynvml`); without it nvidia-smi is used; CPU-only machines work.

## Unverified / pending on your machine
Real GPU readings, `/api/ps` shapes of your Ollama version, the Windows Job Object, benchmarking Qwen3-VL / Qwen3-Embedding / Qwen3-Reranker (those are model-library and hardware questions I cannot answer from here).

## Round 72 (what remained, and was buildable here)
- **Scheduler is now wired in:** `index_documents` runs as P2 and the `tune_ai` self-test as P3 through the shared `resources.scheduler()`; the background upkeep parks P3+ work while gaming / presenting / meeting / power saving (`apply_mode_hold`) and releases it afterwards. Voice and chat inference are NOT routed through it (changing those paths is riskier than the gain).
- **`investigate_problem`** (`core/investigate.py`): symptom -> domains -> evidence (logs, resources, troubleshoot checks, events) -> ranked causes marked CONFIRMED / STRONGLY_SUPPORTED / POSSIBLE / UNKNOWN, safe next steps, and `recheck` after a fix. Repairs still go through the existing approval path.
- **`deadline_risk`** (`core/deadlines.py`): overdue, due soon, behind pace, blocked, overloaded - each with its rule. Not measured: repeated postponement, calendar, effort.
- Not built: Visual grounding rewrite (click tools already read a fresh screenshot / UIA tree and check the foreground window), embedding / reranker evaluation (needs your models), Android items (need a JDK).
