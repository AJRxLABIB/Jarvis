# Round 75 - audit of the "seven upgrades" prompt (Phase 0) and what was built

Checked against the repository first. "Not duplicated" means the existing piece is reused.

| Upgrade | Already in the repo | Built in round 75 | Not built / blocked |
|---|---|---|---|
| 01 Physical-world vision | `AIManager.vision_model/vision_chat`, OCR engines, phone inbox (files from the phone), screen watch | `core/physical_vision.py` (explicit 5-minute camera session, image validation by contents, downscale, **read text kept apart from the model's interpretation**, injected-instruction flagging, measured pixel comparison); tools `camera_session`, `analyze_photo` | Android camera capture screen (not written: send a photo with Share > JARVIS Mobile, then analyse it), YOLO/ONNX object detector, handwriting/table extraction, live video |
| 02 Media intelligence | faster-whisper recognizer, semantic memory, scheduler | Migration 42 (`media_assets`, `media_segments`), `core/media_ffmpeg.py` (the one reviewed place that starts FFmpeg), `core/media_index.py` (resumable steps, cancel, keyword search with file + timestamp, extractive chapters and study notes); tools `media_search`, `media_manage` | Speaker separation, scene detection wired into the index (the parser exists), keyframe/slide analysis, embeddings search, "every recording with this slide" |
| 03 A2A | MCP client/server (round 69), permission engine | `core/a2a.py`: **client only**, A2A v1.0 JSON-RPC (`SendMessage` / `GetTask` / `CancelTask`, `A2A-Version`, agent card at `/.well-known/agent-card.json`, `TASK_STATE_*`), registry with trust (pending/approved/blocked; a changed card goes back to pending), outgoing secret/path filter, rate limits, untrusted-result handling, heuristic conflict hints; tools `a2a_status`, `a2a_manage` (DANGEROUS); off unless `A2A_ENABLED=true` | JARVIS as an A2A **server** (new inbound attack surface), streaming, push notifications, gRPC/REST bindings, agent credentials, an automatic "plan -> several agents" planner |
| 04 Location | nothing (only a note in STATUS) | `core/geofence.py` (places, conservative enter/exit/dwell with accuracy gating and hysteresis, event validation and dedupe, rules that only say or suggest), `core/location_events.py` + a hook in the phone check-in (`report.location_events`); tools `location_status`, `manage_places` | **The Android side**: a Google Play services geofence needs a Gradle/Play-services build this project does not use; local evaluation with `LocationManager` needs background-location permission and a foreground service - not written. Until then nothing produces events |
| 05 AppFunctions | nothing | evaluation only (below) | the implementation |
| 06 Foundry Local | provider registry (`ai/manager.py`), Ollama provider | `ai/foundry_local.py` (OpenAI-compatible: models, chat, streaming, tool calls, vision input), loopback-only, registered as provider `foundry_local`, setting `FOUNDRY_LOCAL_URL`; tool `inference_providers` | automatic Foundry discovery (the port is chosen by its service: you configure it), model download/unload (no API assumed), per-capability auto-routing between engines, Foundry embeddings |
| 07 Sound events | VAD / wake word (speech only) | `core/sound_events.py`: explicit session with an indicator, expiry, windows scored and discarded, rules with threshold/consecutive/cooldown; always-available **loudness** fallback; YAMNet-ONNX wrapper if you place the model; tool `sound_monitor` | Named sounds (doorbell, knock, glass) without a model; no model is downloaded automatically; no phone-side listening |
| 09 Shared layer | `EventBus` (`core/events.py`), `request_context`, scheduler/gateway (rounds 71-74) | eight new event names (payloads carry `correlation_id`, never media or coordinates); `core/context_facts.py` (verified / told by you / historical / model guess / outside source, with expiry) | wiring every result into one orchestrated multi-step task; dashboards/workspaces for the new features (no new Qt screens were built) |

## AppFunctions evaluation (not implemented)
Source: developer.android.com/ai/appfunctions (checked this round). AppFunctions is **experimental**, available on **Android 16+**, and is a platform API plus a **Jetpack library whose
annotation processor generates the schema from Kotlin**. Callers need `EXECUTE_APP_FUNCTIONS`; the page states that the Gemini integration is a private preview with trusted testers and
that apps' functions may not be reachable by system agents yet. JARVIS Mobile is pure Java compiled without Gradle, Kotlin or an annotation processor, so it cannot use the library; hand-writing the
generated XML/service would be guessing at an experimental interface. Revisit when it is stable and the project moves to a Gradle/Kotlin build. Nothing about normal JARVIS Mobile is affected.

## Hardware (every new feature)
| Feature | Minimum | CPU only | Memory / time |
|---|---|---|---|
| Photo analysis | the vision model you already run | yes (slow) | image downscaled to 1280 px; seconds to a minute per photo on CPU |
| Media indexing | FFmpeg on PATH + the existing speech model | yes (slow: roughly real-time or slower on CPU) | working WAV deleted when done; only text is stored |
| A2A | internet to the agent | yes | negligible |
| Geofence / events | none | yes | negligible |
| Foundry Local | Foundry Local installed and running | its own choice | its own |
| Sound events | microphone | yes | one-second windows, a few KB |

## Honest status
All new logic is covered by `tests/test_round75.py` (36 tests) with fakes: no FFmpeg, camera, phone, agent, Foundry service, model or microphone was used. Nothing here has run on real hardware or
against a real A2A agent. `python run.py --doctor` does not yet list the new engines; use the `inference_providers` tool.
