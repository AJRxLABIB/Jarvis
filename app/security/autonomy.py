"""Autonomy levels - how much Jarvis may do on its own. Enforced by the action executor for EVERY caller (GUI, voice,
phone, schedules, agents), never by the model's own reasoning; the model cannot change the level.

* observe  - (level 0) only reads: answers, searches, looks at the screen/files/devices; nothing is changed.
* suggest  - (level 1) reads, and for anything that would change something it describes the step it WOULD take
             ("Suggested, not done: ...") without doing it - a plan you can then run yourself or approve.
* approval - (level 2, supervised) every action that changes something asks first; no standing approvals and no
             "direct command" shortcut.
* routine  - (levels 2-4, the default) low-risk actions run, riskier ones ask; reviewed workflows and approved mission
             plans run with their checkpoints; dangerous steps always pause.

Narrower limits on top of the level (Settings > Security):
* AUTONOMY_REMOTE      - the level for requests from a phone (the stricter of the two applies);
* AUTONOMY_ASK_CATEGORIES   - tool categories that always ask (e.g. "files, phone");
* AUTONOMY_BLOCK_CATEGORIES - tool categories that are switched off entirely (e.g. "home" = smart home).

Emergency stop and switching Jarvis's own windows always work.
"""

from __future__ import annotations

LEVELS = ("observe", "suggest", "routine", "approval")
STRICTNESS = {"routine": 0, "approval": 1, "suggest": 2, "observe": 3}

# tools that only read / report. Anything not listed is treated as changing something.
READ_ONLY = frozenset({
    "activity_timeline", "ask_documents", "ask_history", "ask_knowledge", "browser_read", "calculate",
    "calendar_events", "check_device", "check_project", "code_impact", "command_center", "current_context",
    "daily_briefing", "describe_screen", "dev_environment", "device_info", "diagnose_jarvis", "explain_action",
    "explain_memory", "file_info", "find_files", "find_my_files", "find_duplicates", "get_datetime", "get_news",
    "get_weather", "git_log", "goal_check", "investigate", "research_brief", "goal_review", "capability_report", "anomalies", "git_status", "health_history", "home_status", "integrations", "list_agents",
    "list_automations", "list_contacts", "list_devices", "list_folder", "list_goals", "list_nicknames",
    "list_processes", "list_proposals", "list_todos", "list_undoable", "mission_history", "mission_status",
    "model_status", "pc_health", "program_history", "project_profile", "read_clipboard", "read_code", "read_emails",
    "read_messages", "read_screen", "read_whatsapp", "recall", "recall_activity", "review_changes",
    "review_mistakes", "search_code", "search_everything", "search_history", "security_scan", "show_proposal",
    "simulate_workflow", "system_status", "trace_request", "voice_stats", "what_can_you_do", "what_do_you_know", "request_context", "skill_card", "guardian", "what_changed", "sensors", "plugin_check", "resources", "verification_coverage", "investigate_problem", "deadline_risk", "phone_context", "situations", "attention", "service_impact", "office_info", "interaction_advice",
    "workflow_versions", "web_search", "plan_my_day", "calendar_conflicts", "memory_used", "answer_sources",
})
ALWAYS = frozenset({"emergency_control", "jarvis_control"})       # stopping, and Jarvis's own screens


def normalise(value: str | None) -> str:
    value = (value or "routine").strip().lower()
    return value if value in LEVELS else "routine"


def stricter(a: str | None, b: str | None) -> str:
    """The more cautious of two levels (a phone request uses the stricter of the PC level and the remote level)."""
    a, b = normalise(a), normalise(b)
    return a if STRICTNESS[a] >= STRICTNESS[b] else b


def categories(text: str | None) -> frozenset[str]:
    """'files, Phone ,smart_home' -> {'files', 'phone', 'smart_home'}"""
    return frozenset(p.strip().lower() for p in (text or "").split(",") if p.strip())


def is_read_only(tool: str, arguments: dict | None = None) -> bool:
    if tool == "batch_rename":
        return not (arguments or {}).get("apply")                    # a preview only reads
    if tool == "phone_control":
        return (arguments or {}).get("action") == "status"
    if tool in ("journal", "habits"):
        return (arguments or {}).get("action") in ("list", "search", "today", "report", "streaks")
    if tool == "decision":
        return (arguments or {}).get("action") in ("list", "show", "relevant", "compare")
    if tool == "preferences":
        return (arguments or {}).get("action") in ("list", "questions", "resolve")
    if tool == "handoff":
        return (arguments or {}).get("action") in ("list", "inspect")
    if tool == "task_replay":
        return (arguments or {}).get("action", "diagnose") in ("list", "show", "diagnose", "simulate")
    if tool == "experience":
        return (arguments or {}).get("action", "propose") == "propose"
    if tool == "radar_feedback":
        return (arguments or {}).get("action") == "scan"
    if tool == "learning":
        return (arguments or {}).get("action", "report") in ("report", "lessons")
    if tool == "local_device":
        return (arguments or {}).get("action") in ("status", "list")
    if tool == "expenses":
        return (arguments or {}).get("action") in ("list", "report", "budgets", "trend")
    return tool in READ_ONLY


def describe_step(tool: str, arguments: dict | None) -> str:
    shown = ", ".join(f"{k}={v!r}" for k, v in list((arguments or {}).items())[:6] if v not in (None, "", [], {}))
    return f"{tool}({shown})"


def blocked_in(level: str, tool: str, arguments: dict | None = None, category: str = "",
               blocked_categories: frozenset[str] = frozenset()) -> str | None:
    """Why ``tool`` may not run at this autonomy level at all (None = it may)."""
    if tool in ALWAYS:
        return None
    level = normalise(level)
    if category and category.lower() in blocked_categories and not is_read_only(tool, arguments):
        return (f"'{category}' actions are switched off (Settings > Security > blocked categories). I didn't do "
                f"{describe_step(tool, arguments)}.")
    if level == "observe" and not is_read_only(tool, arguments):
        return ("Observe mode: I only look and read, I don't change anything. Switch to 'routine' in "
                "Settings > Security to let me act.")
    if level == "suggest" and not is_read_only(tool, arguments):
        return (f"Suggested, not done (suggest mode): {describe_step(tool, arguments)}. Say it again in routine mode, "
                "or do it yourself.")
    return None


def must_ask(level: str, tool: str, arguments: dict | None = None, category: str = "",
             ask_categories: frozenset[str] = frozenset()) -> bool:
    """Approval mode (or a category on the always-ask list): everything that changes something asks first."""
    if tool in ALWAYS or is_read_only(tool, arguments):
        return False
    return normalise(level) == "approval" or bool(category and category.lower() in ask_categories)
