"""What works where: the capability matrix (offline / local network / internet / credentials / extra hardware or OS
permission), and which TOOLS change things, need the network, or always ask.

The tool part is generated from the registered tools (never a hand-written list that drifts); the feature part is a
reviewed table. ``python run.py --capabilities`` prints it; docs/CAPABILITIES.md is the same text.
"""

from __future__ import annotations

from collections import defaultdict

# availability classes, from most to least self-contained
OFFLINE = "offline"                     # works with no network at all
OFFLINE_AFTER_SETUP = "offline after setup"   # needs a one-time download (models, voices), then offline
LOCAL_NETWORK = "local network"         # needs your Wi-Fi / LAN (phone, other PCs, smart home), no internet
INTERNET = "internet"
CREDENTIALS = "credentials"             # an account / token you configure first
EXTRA = "hardware / OS permission"      # a GPU, a microphone, Outlook, an Android permission...

FEATURES: tuple[tuple[str, str, str], ...] = (
    ("Cross-device transactions: one task across the PC and a phone with an owner device per step, dependencies, checkpoints, completion only when every step is verified by its own device (a phone step needs a result the phone returned), and recovery choices when a device drops", OFFLINE, "bookkeeping only: the steps are run by the normal tools; needs a paired phone for phone steps"),
    ("Event intelligence: related events (a crashed project, what changed before it, earlier similar events) become one explained situation with next steps and a stated confidence", OFFLINE, "only as good as the events and logs captured; git / history readers are optional"),
    ("Attention firewall and mode scheduling: non-urgent notices wait while a full-screen app, a meeting or a focus block is active; gaming / presentation / power saving / meeting / offline / quiet modes change which background work runs", OFFLINE, "full-screen detection is Windows-only and unverified on hardware; urgent security notices are never delayed"),
    ("What changed while you were away: events, notifications, missions, files in watched folders (names only), devices and security incidents since a time you give", OFFLINE, "file scan is bounded; sources that cannot be read are listed, not skipped silently"),
    ("Dependency-aware health: when one part fails (AI engine, database, disk) say what still works, what is limited and what is unavailable", OFFLINE, "static dependency table; repairs still need your approval"),
    ("Verified project start: start the project's usual command with no window, wait, then say whether it is really running (its port answers) or crashed, with the cause read from its log (missing package, port in use, syntax error, ...) and the next step; also status, log tail and stop", OFFLINE, "needs the project's own tools installed; Windows; logs are untrusted text"),
    ("Cyber guardian (off by default): notices bursts of file changes in your folders, new startup programs, new network listeners and stale / failed backups; records incidents with evidence and confidence and recommends steps - it never stops or deletes anything and is not antivirus", OFFLINE, "Windows readers unverified here; names and times only, never file contents"),
    ("Device mesh: one list of this PC, other computers and paired phones with trust, availability, battery, abilities and pending handoffs; optional local-network announcement and scan (mDNS) - found devices are never trusted until paired", LOCAL_NETWORK, "announcing is off by default (LOCAL_DISCOVERY); some Wi-Fi networks block multicast"),
    ("Notification intelligence: phone notifications labelled critical / suspicious / action required / important / routine / promotional, repeats and promotions filtered, deadlines noticed, digest on request", OFFLINE, "reads the words, not the sender; rules, not a model"),
    ("Skill card: inputs, needed tools / apps / abilities, risk and failure handling per step, undo support, run history, and what is not recorded", OFFLINE, "read-only; editing is the Workflow Studio"),
    ("More operating modes: deep work, study, travel, sleep (only quieter; never grant anything)", OFFLINE, ""),
    ("Unified request context: one merged view per request (activity, projects, memories, missions, devices, resources, permissions) with evidence labels, age, budgets, and a focused question instead of a guess; shared agent blackboard stops repeated side effects, busy collisions and delegation loops", OFFLINE, "local reads only"),
    ("Reusable skills: workflows with named inputs, proposed from your repeated routines", OFFLINE, "saving asks you"),
    ("Measured model comparison (speed, load time, GPU share) with priority-based recommendation", OFFLINE, "Ollama; numbers need your PC"),
    ("Task debugger: timeline, first divergence, simulation, safe live re-run of read-only calls", OFFLINE, "reads the assistant's own record"),
    ("Tool foundry: a request becomes a validated, previewed, mock-tested workflow of existing tools (never generated code)", OFFLINE, "needs the local model to draft from a request; a spec works without"),
    ("Workspaces: folder + files + layout + goal + mission, preview of what restoring opens and what is missing", OFFLINE, "layout restore needs Windows and the apps"),
    ("Retrieval controls: exclusions, source switches, calendar/notes/page readers, near-duplicate report", OFFLINE, "lexical similarity only"),
    ("Mixed-language command set with unsafe-rewrite and negation checks", OFFLINE, "command layer only; Bangla speech recognition needs real audio"),
    ("Screen watch: tell me when a window changes or a phrase appears (explicit, time-limited, private, backs off, indicator on)", OFFLINE, "Windows; uses screen capture + local OCR"),
    ("Hand-gesture mouse control from the webcam (index point, pinch click, optional drag / right click / dwell click, two-finger scroll, two-hand pinch zoom, palm swipes, open-palm pause; off by default, local, camera only while on, emergency stop)", OFFLINE_AFTER_SETUP, "needs mediapipe + a one-time model download (--setup-hand-model), a webcam and Windows; real tracking accuracy is verified on your PC"),
    ("Performance modes (Fast / Balanced / High quality / Battery saver / Auto) from measured memory, CPU, GPU memory and battery: model size choice, background chores, gesture frame rate, and 'will this model fit' with numbers", OFFLINE, "numbers come from this PC; GPU memory only for NVIDIA via nvidia-smi"),
    ("Work-state snapshots: remember a session, compare with the real PC, resume safely (nothing replayed, nothing closed); recovery report of unfinished missions with uncertain steps", OFFLINE, "window titles are never stored"),
    ("Finance planning: preview/import spending CSV (no doubles), recurring payment reminders, savings targets, month-end forecast and what-if scenarios with assumptions shown", OFFLINE, "local only; no bank access, nothing is ever paid"),
    ("Data refinery: scan folders for deadlines, tasks, decisions and labelled values with source file/line/version, stale + conflict detection, review before trust", OFFLINE, "rules, not a model; nothing is trusted until you confirm"),
    ("Optional wellness log (off by default): sleep, exercise, breaks, energy, mood with plain-statistics patterns", OFFLINE, "numbers you tell it only; not medical advice"),
    ("Move sound between PC speakers, headphones and a Bluetooth / connected speaker by voice (falls back to the PC speakers; result read back from Windows)", OFFLINE, "Windows; uses the undocumented IPolicyConfig interface that every audio switcher uses; not run on real devices by the tests"),
    ("Hardware validation: run real checks for Windows UI Automation / scaling / monitors, voice latency, and your phone (--validate)", OFFLINE, "must be run on your PC; pending is never a pass"),
    ("Digital twin: last observations with source and age (stale marked), and what-if preview of a plan before it runs", OFFLINE, "local reads only; estimate, not a guarantee"),
    ("Anomaly detection and repeated-routine proposals (from your own history)", OFFLINE, "needs some history; proposals never save themselves"),
    ("Chat and tool use with a local model (Ollama)", OFFLINE_AFTER_SETUP, "Ollama + a downloaded model"),
    ("Wake word, speech recognition", OFFLINE_AFTER_SETUP, "speech models downloaded once; a microphone"),
    ("Spoken replies (Piper / Kokoro voices)", OFFLINE_AFTER_SETUP, "voice files downloaded once"),
    ("Neural Edge voices (en-GB-Ryan, bn-BD)", INTERNET, "Microsoft's online voice service; falls back to Piper"),
    ("Open / arrange / close apps and windows", OFFLINE, "Windows"),
    ("Files: find, duplicates, batch rename with preview, zip", OFFLINE, "folders you allow"),
    ("Screen reading (OCR) and window overview", OFFLINE, "Windows OCR / installed OCR engine"),
    ("Screenshot understanding (vision model)", OFFLINE_AFTER_SETUP, "a vision model in Ollama"),
    ("Memory, journal, habits, expenses, to-dos, reminders", OFFLINE, "the local database"),
    ("Local document search (full text)", OFFLINE, "folders you index"),
    ("Semantic document search (embeddings)", OFFLINE_AFTER_SETUP, "an embedding model in Ollama"),
    ("Automations, workflows, missions, scheduler", OFFLINE, "Jarvis running (or the background host)"),
    ("System monitor, PC health, diagnostics", OFFLINE, "Windows; GPU figures need nvidia-smi"),
    ("JARVIS Mobile: chat, approvals, cancel, phone link", LOCAL_NETWORK, "paired phone on the same Wi-Fi"),
    ("Phone flashlight / ring / open app (phone link)", LOCAL_NETWORK,
     "JARVIS Mobile with 'Let my PC control this phone' on"),
    ("Phone notifications on the PC", LOCAL_NETWORK, "Android notification access (you grant it on the phone)"),
    ("Instant phone updates while the app is open (no Firebase)", LOCAL_NETWORK, "JARVIS Mobile 1.1 open on the phone"),
    ("See / tap the phone's screen from the PC", LOCAL_NETWORK,
     "JARVIS Mobile 1.1: Share screen (Android asks each time); taps need its Accessibility service"),
    ("Phone: call, SMS, read messages, contacts, gallery / camera", LOCAL_NETWORK,
     "JARVIS Mobile 1.2 with 'Allow calls and messages' + Android permissions; calls/SMS ask you first"),
    ("Phone: read its screen, tap by text, clipboard both ways, files to the PC, locate it", LOCAL_NETWORK,
     "JARVIS Mobile 1.3; screen/taps need its Accessibility service, location needs Location allowed"),
    ("Phone: lock its screen from the PC; automations when it is plugged in / unplugged / low on battery", LOCAL_NETWORK,
     "JARVIS Mobile 1.4; locking needs YOU to activate it as a device admin on the phone (lock is its only power)"),
    ("Command center view: left rail, conversation, task timeline, devices, security, automation inspector", OFFLINE, "nothing extra (Ctrl+Shift+C)"),
    ("Strict offline mode: connections outside this PC and your network are refused at the socket (OFFLINE_MODE=on)", OFFLINE, "nothing extra; a configured web proxy can forward traffic the guard cannot see"),
    ("Hand a mission to a paired phone as a signed, expiring, single-use package", LOCAL_NETWORK, "JARVIS Mobile; the PC side is built, the phone screen for it is not"),
    ("Hard questions use a bigger installed model when it fits the GPU", OFFLINE_AFTER_SETUP, "a bigger model in Ollama"),
    ("Teach a routine: Jarvis saves what it did as a workflow (never your keys or mouse)", OFFLINE, "nothing extra"),
    ("Encrypted database backup to a USB drive (password, AES-256-GCM)", OFFLINE, "nothing extra; scripts/encrypted_backup.py"),
    ("Expenses from bank / bKash / Nagad SMS", LOCAL_NETWORK, "JARVIS Mobile with 'Allow calls and messages'"),
    ("Clipboard AI (summarise / translate / fix what you copied)", OFFLINE_AFTER_SETUP, "a chat model in Ollama"),
    ("Excel automation (read / write / add rows / create)", OFFLINE, "Microsoft Excel installed"),
    ("Keep your voice commands to train the recognizer", OFFLINE, "SPEECH_DATASET on; training needs a GPU"),
    ("Wake another PC (Wake-on-LAN)", LOCAL_NETWORK, "wired network + WoL enabled in the PC's BIOS"),
    ("Smart home (Home Assistant)", LOCAL_NETWORK, "your Home Assistant URL + token"),
    ("Web search, news, weather, research", INTERNET, "internet"),
    ("Outlook calendar and mail", EXTRA, "classic Outlook desktop app signed in (mail sync needs internet)"),
    ("Google Calendar / Drive", CREDENTIALS, "your own Google OAuth client id (optional, off by default)"),
    ("GitHub (issues / pull requests)", CREDENTIALS, "a GitHub token (optional)"),
    ("Push wake-ups to the phone (FCM)", CREDENTIALS, "your own Firebase service account (optional)"),
)


def tool_rows(registry) -> list[dict]:
    from app.security import autonomy
    from app.security.permissions import RiskLevel

    rows = []
    for tool in sorted(registry.tools, key=lambda t: (t.category, t.name)):
        rows.append({"name": tool.name, "category": tool.category, "risk": RiskLevel(tool.level).name.lower(),
                     "reads_only": autonomy.is_read_only(tool.name), "network": bool(tool.needs_network)})
    return rows


def markdown(registry) -> str:
    lines = ["# What works where", "",
             "Generated by `python run.py --capabilities` from the tools that are really registered.", "",
             "## Features", "", "| Feature | Works | Needs |", "|---|---|---|"]
    lines += [f"| {f} | {a} | {n} |" for f, a, n in FEATURES]
    by_category: dict[str, list[dict]] = defaultdict(list)
    for row in tool_rows(registry):
        by_category[row["category"]].append(row)
    total = sum(len(v) for v in by_category.values())
    lines += ["", f"## Tools ({total})", "",
              "*risk*: safe runs; moderate asks unless pre-approved; dangerous always asks; critical is blocked. "
              "*reads only*: allowed in observe / suggest mode. *network*: refused while offline.", ""]
    for category in sorted(by_category):
        lines += [f"### {category}", "", "| Tool | Risk | Reads only | Network |", "|---|---|---|---|"]
        for row in by_category[category]:
            lines.append(f"| {row['name']} | {row['risk']} | {'yes' if row['reads_only'] else ''} | "
                         f"{'yes' if row['network'] else ''} |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
