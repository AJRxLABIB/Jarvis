# Round 74 - audit of the "Next-Generation" master prompt, and what was built

The repository was checked first; most of the prompt already exists. Only genuine gaps were built.

| Prompt section | Status in the repository | Round 74 |
|---|---|---|
| A. Inference orchestrator | `core/resources.py`: GPU read, `plan_load`, model book, P0-P4 scheduler, VRAM reservations, mode hold, CPU fallback decision. Not every inference path used it; no request object or lifecycle. | **`core/inference_gateway.py`**: request abstraction (id, capability, model, priority, memory estimate, timeout, context, device, format, retries), states QUEUED / RESOURCE_CHECK / MODEL_PREPARATION / RUNNING / PAUSED / COMPLETED / FAILED / CANCELLED, one load per model, CPU fallback with the reason kept, bounded retries, cancel, bounded history, `inference_queue` tool. **Chat and voice are not routed through it yet.** |
| B. Failure investigator | `core/replay.py` (timeline, earliest divergence), `core/investigate.py`, `core/retry.py`, `core/capability_gaps.py`. | **`core/root_cause.py`**: twelve cause classes with evidence and a confidence that is never "certain", later failures marked "possibly consequences", recovery options with levels 0-4 (4 = forbidden: never offered, never run), `root_cause` tool. Proposes only; runs nothing. |
| C. Android 2.0 | Command Center, NSD, sensors, tiles (round 73), signed commands, pinned TLS, long-poll, mission view, Wake-on-LAN (`core/devices.py`). | Nothing new. Still **uncompiled** until `packaging\build_android.ps1` is run. |
| D. Plugin sandbox | Import-time probe in a restricted child + Windows Job Object (rounds 70-71; fixed in round 73 follow-up). | Nothing new. AppContainer / a runtime permission broker are **not** built; the Job Object limit is unverified on Windows. |
| E. Knowledge hub | PDF/DOCX/XLSX/text indexing, knowledge graph, near-duplicate detection, privacy zones, memory controls. | **`core/vault.py`** + `vault_scan` tool: Obsidian-style vault (front matter, tags, wiki links, backlinks, unresolved links; skips hidden/excluded/oversized). Not yet wired into the index or graph. Browser bookmarks / git history indexing not built. |
| F. Mission supervisor | `core/missions.py` (planned/running/paused/verifying/failed/...), checkpoints, recovery, goal contracts. The prompt's exact state names (AWAITING_APPROVAL, RECOVERING) are not separate states. | Nothing new. |
| G. Voice 2.0 | Whisper/VAD/wake word, correction learning, speaker check, voice validation harness. | Nothing new. |
| Guardian, twin, visual operator, life intelligence | Exist (rounds 62-72). | Nothing new. |

## Recovery levels (root_cause)

0 observe only, 1 harmless diagnostic, 2 reversible change (needs authorisation by policy), 3 high impact (explicit confirmation), 4 forbidden. `level_of` classifies an
action from its wording; wording it does not recognise is treated as a change (2), never as harmless. Only levels 0-1 may run unasked.

## Honest status
Logic and tests with fakes (`tests/test_round74.py`, 25 tests). No real GPU, no Ollama, no real run records beyond hand-built ones. The wording-based level classifier is a
heuristic, not a security boundary: the executor's permission pipeline remains the authority.
