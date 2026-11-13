"""What each tool-backed command-center panel shows and offers (no Qt here, so it is tested).

A panel is a list of read-only BLOCKS (a label + one existing tool call whose answer is shown as text) and ACTIONS (buttons that run an existing tool as your
own command: same permission checks, confirmations and audit as saying it in chat). Nothing is computed in the UI: if a number is shown, a tool made it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Block:
    label: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Action:
    label: str
    tool: str
    args: dict[str, Any]
    tip: str = ""


@dataclass(frozen=True)
class Panel:
    key: str
    title: str
    intro: str
    blocks: tuple[Block, ...]
    actions: tuple[Action, ...] = ()
    extras: tuple[str, ...] = ()          # names of extra window openers the host may add as buttons (e.g. "open_workflow_studio")


RETENTION_STORES = ("notifications", "events", "clipboard", "metrics", "activity", "transactions", "incidents")

PANELS: tuple[Panel, ...] = (
    Panel("coordination", "Coordination",
          "Tasks that span your PC and phone, related events explained as one situation, and what changed while you were away. Read from the same records the "
          "chat tools use; a phone step counts as done only when the phone reported it.",
          (Block("Cross-device tasks", "transaction", {"action": "list"}),
           Block("Situations", "situations", {"limit": 5}),
           Block("What changed while you were away", "what_changed", {"mark": False}),
           Block("What still works if something fails", "service_impact", {}),
           Block("Would I interrupt you now?", "attention", {})),
          (Action("Check what the phone reported", "transaction", {"action": "reconcile"}, "Settle running phone steps from the phone's own results"),
           Action("What changed since yesterday", "what_changed", {"since": "yesterday", "mark": False}),
           Action("I'm back: mark me active now", "what_changed", {"since": "1 minute", "mark": True}, "The next 'what changed' starts from now"))),
    Panel("guardian", "Guardian",
          "The cyber guardian watches for file-change bursts, new startup programs, new network listeners and stale backups. It reports and recommends; it never "
          "stops or deletes anything and it is not antivirus.",
          (Block("Status", "guardian", {"action": "status"}), Block("Open incidents", "guardian", {"action": "incidents"})),
          (Action("Scan now", "guardian", {"action": "scan"}, "Look right now (the first scan only records what is normal)"),
           Action("Treat what is running now as normal", "guardian", {"action": "accept_baseline"}, "Stops reporting current startup entries and listeners"))),
    Panel("skills", "Skills",
          "Your saved routines as honest cards: what each needs, which steps are risky, what happens if a step fails, what can be undone. Editing happens in the "
          "Workflow Studio.",
          (Block("Saved routines", "skill_card", {}), Block("Tools you could add (capability gaps)", "capability_report", {})),
          (), ("open_workflow_studio",)),
    Panel("learning", "Learning",
          "What Jarvis learned from using it, whether it is improving, and which learned items hurt, contradict a better one or faded. Cleanup only retires items "
          "(your request history is kept) and can be undone.",
          (Block("Is it improving?", "learning", {"action": "report"}),
           Block("Check for items that hurt", "learning_audit", {"action": "audit"}),
           Block("Corrected mistakes used as tests", "learning_audit", {"action": "hard_cases"}),
           Block("Past cleanups", "learning_audit", {"action": "history"})),
          (Action("Retire items that hurt...", "learning_audit", {"action": "cleanup"}, "Asks first; reversible"),
           Action("Undo the last cleanup", "learning_audit", {"action": "undo"}))),
    Panel("retention", "Data kept",
          "What Jarvis keeps, how long, and how much. Clearing removes one store only; backups, log files and exported files are separate copies.",
          (Block("What is kept", "retention", {"action": "status"}),),
          tuple(Action(f"Clear {s}...", "retention", {"action": "clear", "store": s}, "Asks you to confirm first") for s in RETENTION_STORES)),
)
PANELS += (
    Panel("vision", "Vision",
          "Photos from your phone's camera, analysed on this PC only: the text read from the photo, the objects an optional detector recognised, and what the vision "
          "model says it sees - shown apart, because only the first two are evidence. Nothing is recorded in the background; on the phone: JARVIS Mobile > Command center > "
          "Take a photo for JARVIS.",
          (Block("Photo session", "camera_session", {"action": "status"}),),
          (Action("Analyse the newest photo from my phone", "analyze_photo", {}, "Photos taken with 'Take a photo for JARVIS' work for 10 minutes; other shared images need a session"),
           Action("Open a 5-minute photo session", "camera_session", {"action": "start"}),
           Action("Close the photo session", "camera_session", {"action": "stop"}))),
    Panel("media", "Media",
          "Recordings (lectures, meetings, videos) that Jarvis has indexed so you can ask where something was said. Files stay where they are; needs FFmpeg for new ones. "
          "Speaker labels (\"Speaker 1/2\") are optional and only approximate.",
          (Block("What is indexed", "media_search", {"action": "status"}),),
          (Action("Index the next recordings", "media_manage", {"action": "process"}, "Runs in the background and can be cancelled"),
           Action("Index with speaker labels", "media_manage", {"action": "process", "speakers": True}, "Extra time; labels are approximate"),
           Action("Cancel indexing", "media_manage", {"action": "cancel"}))),
    Panel("agents", "Agents",
          "Outside AI agents (A2A) that Jarvis may hand ONE task to, only after you approved each one; their answers are treated as data, never as instructions. "
          "Register and approve them by asking in chat (\"register the agent at https://...\").",
          (Block("Registered agents", "a2a_status", {}),)),
    Panel("places", "Places",
          "Places Jarvis knows (names and sizes only, never coordinates) and the rules that fire when you arrive, leave or stay. Your phone decides arrivals itself about every "
          "15 minutes and reports only the event; alerts can be about 30 minutes late.",
          (Block("Saved places and rules", "location_status", {}),),
          (Action("Send my places to the phone", "manage_places", {"action": "send_to_phone"}, "Then switch on Place alerts in JARVIS Mobile > Command center"),)),
    Panel("sound", "Sound",
          "Listening for configured sounds (loud noise by default; named sounds need the YAMNet model you put on disk). Opt-in, stops by itself, nothing is recorded, and it "
          "is not a safety or emergency system.",
          (Block("Listening status", "sound_monitor", {"action": "status"}),),
          (Action("Listen for loud sounds (30 min)", "sound_monitor", {"action": "start"}, "Asks first; the microphone indicator stays on"),
           Action("Stop listening", "sound_monitor", {"action": "stop"}))),
    Panel("daily", "Daily",
          "Your day in one place: what needs you across mail, WhatsApp, notifications, promises and to-dos; the promises you made and what you are waiting for; "
          "the air, UV and rain around you; and a read-only check-up of this PC. Mail is read over IMAP when IMAP_USER / IMAP_PASSWORD are set in .env "
          "(nothing is ever marked read or changed).",
          (Block("What needs you now", "needs_me", {}),
           Block("Promises and waiting-for", "commitments", {"action": "list"}),
           Block("Around you today", "environment_report", {}),
           Block("PC check-up", "pc_checkup", {})),
          (Action("Scan recent mail for promises", "commitments", {"action": "scan"}, "Reads received and sent mail (read-only) and updates the list"),)),
    Panel("governance", "Trust & learning",
          "How far Jarvis may go and how well it knows itself: mission permission leases (they only restrict), its prediction track record (what it expected vs what "
          "really happened), technical problems it has seen with what failed and what worked, and evidence-backed opportunities. Nothing here acts on its own.",
          (Block("Opportunities", "opportunities", {"action": "list"}),
           Block("Prediction track record", "predictions", {"action": "report"}),
           Block("Open predictions", "predictions", {"action": "list"}),
           Block("Last mission report", "mission_report", {"action": "report"}),
           Block("Mission statistics", "mission_report", {"action": "analytics"}),
           Block("Mission leases", "lease", {"action": "list"}),
           Block("Technical experiences", "tech_experience", {"action": "list"}),
           Block("Memory merge proposals", "memory_merge", {"action": "list"}),
           Block("Idle-time work", "idle_work", {"action": "status"})),
          (Action("Run idle housekeeping now", "idle_work", {"action": "run_now"}, "Local only: prunes old records and refreshes the opportunity list"),
           Action("Export the last request's trace (OpenTelemetry)", "trace_export", {}, "Writes a local file; arguments are left out"))),
    Panel("capture", "Screen history & click teaching",
          "Two opt-in recorders, both off until you start them. Screen history keeps the TEXT of the window in front (never pictures) so you can ask what was on "
          "screen earlier; it needs SCREEN_CAPTURE_ENABLED and SCREEN_HISTORY_ENABLED in .env. Click teaching records which named controls you click during a "
          "session you start - never keys. Password managers, banking, private browsing, meetings and password fields are skipped by both.",
          (Block("Screen history", "screen_history", {"action": "status"}),
           Block("Click teaching", "demo_record", {"action": "status"})),
          (Action("Start screen history", "screen_history", {"action": "start"}, "Asks first; the capture indicator stays on"),
           Action("Stop screen history", "screen_history", {"action": "stop"}),
           Action("Delete all stored screen text", "screen_history", {"action": "delete_all"}, "Asks first; cannot be undone"),
           Action("Stop recording clicks", "demo_record", {"action": "stop"}))),
    Panel("analytics", "Intelligence analytics",
          "How Jarvis is actually doing, from its own records - nothing here is estimated or made up. A block says \"not enough history\" until there is enough to count.",
          (Block("Missions: completion and failing steps", "mission_report", {"action": "analytics"}),
           Block("Predictions: said vs happened", "predictions", {"action": "report"}),
           Block("Which tools verify their results", "verification_coverage", {}),
           Block("Experiments: hypotheses and how well supported", "experiment_log", {"action": "list"}),
           Block("Paused online services", "service_breakers", {"action": "status"}),
           Block("AI requests running and recent", "inference_queue", {}),
           Block("GPU, memory and loaded models", "resources", {})),
          (Action("Reset paused services", "service_breakers", {"action": "reset"}, "Lets every paused online service be tried again now"),)),
    Panel("providers", "AI engines",
          "Which local AI engines are available (Ollama, Foundry Local), how they answer, and which one is used. Nothing is sent to the internet by choosing one.",
          (Block("Engines", "inference_providers", {}),)),
)
BY_KEY = {p.key: p for p in PANELS}
