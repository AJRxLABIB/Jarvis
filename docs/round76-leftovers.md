# Round 76 - the items round 75 left out

What was built, how it was checked, and what is still unverified. Same rule as before: only what a test or a compiler actually ran is called verified.

## Phone: place alerts (`Places.java`, JARVIS Mobile 1.6.0)

The PC sends the phone a short list of places (`set_places`, validated: 1-50 places, 50-5000 m). The phone decides by itself whether you arrived, left or stayed and reports only the event in its next check-in (`location_events`; no coordinates ever leave the phone). Rules are the PC's `core/geofence.py`, ported: a position worse than 100 m (or than the place's radius) says nothing; the first good position only learns where you are; two good positions in a row are needed (leaving also needs a 25 % margin); the same event for one place is not repeated within 5 minutes; 10 minutes inside gives one dwell. Events stay on the phone until a check-in succeeds; the PC ignores repeats by id.

Honest limits: this is not a real-time geofence. The background job looks at the last known location about every 15 minutes (Android decides the exact time) and two looks are needed, so an alert can be up to ~30 minutes late. Android 10+ also needs Location set to "Allow all the time" (`ACCESS_BACKGROUND_LOCATION`, new in the manifest) for looks while the app is closed. It is off until you switch on Command center > Place alerts. On the PC: `manage_places` action `send_to_phone` queues the places; a Places panel shows them.

## Phone: take a photo for JARVIS (`PhotoSend.java`)

Command center > "Take a photo for JARVIS" opens the system camera app (its own preview and shutter; JARVIS Mobile never opens the camera by itself). One photo is shrunk (longest side 1600 px, JPEG, EXIF rotation applied), sent through the same signed `/api/mobile/upload` as Share > JARVIS Mobile, and the copy in Pictures/JARVIS is deleted. Needs Android 10+ (it uses MediaStore for the output file). On the PC, `analyze_photo` accepts a `camera-*` photo younger than 10 minutes without a photo session, because tapping the shutter is the consent; any other shared image still needs `camera_session start`.

## Speaker turns (`core/speaker_turns.py`)

"Speaker 1 / Speaker 2" turns from the recording's audio: 2 s windows, a small fingerprint (13 mel-cepstral numbers + pitch), average-linkage grouping, silence ends turns, tiny groups (the blended window at a hand-over) join the nearest real group. Optional (`media_manage action=process speakers=true`, sensitivity low/normal/high), stored as `kind='speaker'` rows (no new migration), shown beside search hits as "[Speaker 2, approximate]". A failure never loses the transcript; cancel works.

Honest limits: it does not know who anyone is, cannot separate overlapping speech, confuses similar voices, and splits one person when the microphone or room changes. The threshold was tuned on synthetic voices only (they are ~90 apart, same voice ~1); on real recordings expect to adjust `sensitivity`. Not run on a real recording.

## Object detector (`core/object_detect.py`)

Optional YOLOv8/YOLO11-style ONNX model that you place at `data/models/yolo/yolo.onnx` (optional `labels.txt`; COCO names by default). Nothing is downloaded. `analyze_photo` appends "Objects the detector recognised: ..." with counts, confidences and rough position, labelled apart from the OCR text and the vision model's description. Other output layouts are refused with a clear message. Letterbox, decoding and non-maximum suppression are tested with a fake model; real speed and accuracy are not.

## JARVIS as an A2A server (`core/a2a_server.py`, off by default)

`A2A_SERVER_ENABLED=true` plus `python run.py --a2a-server-token` (shown once; only its hash is stored). Agent card at `http://127.0.0.1:8767/.well-known/agent-card.json`, JSON-RPC at `/a2a` with `Authorization: Bearer`. Same trust model as the local MCP server: loopback only, Host/Origin checks, whitelist of SAFE tools with no argument-dependent risk (`A2A_SERVER_TOOLS`, default the same few status tools), normal executor, redacted and capped answers, rate limit 20/min, 64 KB bodies, bounded in-memory task list. SendMessage / GetTask / ListTasks / CancelTask; streaming, push and the extended card answer the spec's UnsupportedOperation / ExtendedAgentCardNotConfigured errors; `A2A-Version` other than 1.0 -> VersionNotSupported (-32009).

Deliberate design choice: **no free-text instruction path**. A message is accepted only as a data part `{"skill": "<id>", "arguments": {...}}` (or a text that is exactly a skill id); anything else is REJECTED with the skill list, because free text from another agent would be a prompt-injection channel into the assistant. Written from the published v1.0 spec; tested with local clients and this app's own A2A client reading the card. Not tried against a third-party A2A agent.

## Qt screens

Six new command-center panels from the existing data-driven `panel_specs` (Vision, Media, Agents, Places, Sound, AI engines): each block and button runs an existing tool as your own command, so permissions, confirmations and audit are unchanged. A test checks every tool and argument is real. Drawing is not exercised without a display.

## AppFunctions: still not built (decision, not an oversight)

Verified facts: experimental, Android 16+, the platform API plus a Jetpack library whose annotation processor is Kotlin (`@AppFunctionDeclaration`, `@AppFunctionSerializable`, `AppFunctionService`); callers need `EXECUTE_APP_FUNCTIONS`; Gemini integration is a private preview. This app is pure Java built without Gradle (`packaging\build_android.ps1`: aapt2 + javac + d8), so the library cannot be used in it, and a separate companion APK cannot read this app's pairing store. Writing unverifiable Kotlin against a moving experimental API would be guessing. If you move the phone app to Gradle/Kotlin, the natural first functions are read-only (`getPcStatus`, `getNextReminder`) calling the same signed command path as the app's Overview screen; say so and it can be added then.

## Verification

* `tests/test_round76.py` - 27 tests: Android wiring checks, a **real compile of every Android source against the API stand-ins plus the JVM test run** (needs a JDK; skipped otherwise), speaker turns on synthetic voices, detector with a fake model, A2A server including hostile loopback HTTP cases, panels.
* The JVM test (`android/JarvisMobile/test/SyncClientTest.java`) now also covers the place-alert rules (arrive, leave, vague fixes, cooldown, dwell).
* New this round: the stand-in Android API (`test/android_api`) was missing classes used by the round-73 sources (sensors, NSD, tiles, CheckBox), so `compile_all.sh` could not have passed; they were added and **all Android sources now compile against the stand-ins**. That proves types and imports, not that Android behaves as assumed - `packaging\build_android.ps1` is still the real compiler, and nothing has run on a phone.
