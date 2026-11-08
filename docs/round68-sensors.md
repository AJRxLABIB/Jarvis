# Round 68 - physical sensors (MQTT) and plugin capability scan

## Audit of the "JARVIS 4.0" prompt against the repo
Already present (not rebuilt): device mesh/registry/discovery, signed handoff, Wake-on-LAN and phone PC control, plugin manifests (sha256 pin, per-tool risk), MCP client, Home Assistant + Shelly/Tasmota/WLED, vision/OCR/screen verify, voice pipeline + validation harness, agent contracts, digital twin + what-if, knowledge graph/semantic search, radar/proactive, foundry, replay debugger, guardian, perf profiles + benchmark, unified context engine, offline guard.
Genuine gaps built now: (1) MQTT sensors for ESP32 / ESPHome / Zigbee2MQTT, (2) a static capability scan of plugin files.
Not built (and why): Android dashboard screens / NSD (no JDK here), Matter/Thread (needs a controller such as Home Assistant - already supported), a controlled MCP *server* (a new remote attack surface; not added without your decision), actuator publishing over MQTT (use `smart_home` / `local_device`, which ask first and verify).

## Sensors
Create `config/sensors.json`:

    {"broker": "192.168.1.5:1883",
     "sensors": [
       {"name": "room temperature", "topic": "home/room/temp", "kind": "temperature", "unit": "C", "max_age_s": 900},
       {"name": "office door", "topic": "home/office/door", "kind": "door", "max_age_s": 3600},
       {"name": "humidity", "topic": "home/room/sensor", "kind": "humidity", "key": "humidity"}]}

Kinds: temperature, humidity, light, distance, number, door, motion, switch. Say "check my sensors" / "is the office door open" (tool `sensors`).
Rules: broker must be private / `.local`, anonymous (a password broker is reported, never stored); a reading older than `max_age_s` is "too old to trust", a silent sensor is "unknown", an impossible value (e.g. 140 % humidity) is "invalid". Read-only.

## Plugin scan
Tool `plugin_check` lists, per plugin file, what the source does (network / process / native / dynamic / filesystem) without importing it. Trusting a plugin now also records the detected list as `capabilities` in its manifest. A static scan is a review aid, not a sandbox.

## Unverified
Tested against a fake broker on loopback only. Real broker, ESPHome payload shapes and long-running behaviour need your hardware.

## Round 70 additions
- **Sensor rules** (`config/sensor_rules.json`, notify-only): `[{"name":"door left open","sensor":"office door","op":"=","value":"open","for_s":600,"cooldown_min":60}]`. Fires only from fresh valid readings; stale / unknown / invalid readings reset the timer. Checked every ~2 minutes by the background upkeep (not in gaming / meeting / power-saving modes).
- **Twin + radar:** the twin observes sensors that read OK (900 s lifetime); the radar warns about a fresh temperature >= 35 C or <= 5 C.
- **Weighted hard cases:** corrected mistakes count double in the `tune_ai` self-test.
- **Plugin trust is probed in a child process** (`core/plugin_sandbox.py`): network, process starts, native loads and file writes/deletes are blocked while the plugin loads; a plugin that tries any is refused. Import-time containment only; after trusting, tools run with your rights.
- **Learning panel** in the command center (Learning: report, audit, hard cases, cleanup, undo). Never rendered here: unverified.
- Not built: per-file "who changed it" auditing (Windows gives no cheap per-process file attribution without ETW / admin rights; the guardian still reports bursts, hashes and open-file holders).
