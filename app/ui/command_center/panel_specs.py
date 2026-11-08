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
BY_KEY = {p.key: p for p in PANELS}
