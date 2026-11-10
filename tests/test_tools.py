from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import Field

from app.agent.tool_manager import ToolExecutor, ToolRegistry
from app.ai.base import ToolCall
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.memory.audit import AuditLog
from app.memory.database import Database
from app.security.confirmations import ConfirmationRequest, PendingConfirmer
from app.security.permissions import Decision, PermissionEngine, RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult
from app.tools.basic import get_datetime, safe_eval
from app.tools.catalog import build_default_registry

NOW = datetime(2026, 9, 23, 13, 2, tzinfo=timezone(timedelta(hours=6)))


class Recorder:
    def __init__(self) -> None:
        self.calls: list = []


class PathArgs(ToolArgs):
    path: str = Field(max_length=50)


class ScriptedConfirmer:
    def __init__(self, answer: bool) -> None:
        self.answer, self.requests = answer, []

    async def confirm(self, request: ConfirmationRequest) -> bool:
        self.requests.append(request)
        return self.answer


def make_tools(rec: Recorder) -> list[Tool]:
    def handler(args, ctx):
        rec.calls.append(args)
        return ToolResult.success("done", path=getattr(args, "path", None))

    async def slow(args, ctx):
        await asyncio.sleep(5)

    def crash(args, ctx):
        raise RuntimeError("boom api_key=SECRET123456")

    def friendly(args, ctx):
        raise ToolError("Photoshop is not installed on this computer.", "not_found")

    return [
        Tool("read_thing", "safe read", handler, PathArgs, RiskLevel.SAFE),
        Tool("move_thing", "moderate", handler, PathArgs, RiskLevel.MODERATE),
        Tool("delete_thing", "dangerous", handler, PathArgs, RiskLevel.DANGEROUS,
             confirmation=lambda a, c: f"Delete {a.path}? This may not be recoverable."),
        Tool("format_disk", "critical", handler, PathArgs, RiskLevel.CRITICAL),
        Tool("maybe_risky", "risk depends on args", handler, PathArgs, RiskLevel.SAFE,
             risk=lambda a, c: RiskLevel.DANGEROUS if "system32" in a.path.lower() else RiskLevel.SAFE),
        Tool("slow_thing", "slow", slow, timeout=0.2),
        Tool("crash_thing", "crashes", crash),
        Tool("friendly_fail", "fails nicely", friendly),
        Tool("web_thing", "needs internet", handler, PathArgs, needs_network=True),
    ]


def executor(tmp_path: Path, rec: Recorder, *, confirm: bool = True, perms: dict | None = None, **settings):
    db = Database(tmp_path / "a.db")
    db.initialize()
    confirmer = ScriptedConfirmer(confirm)
    ex = ToolExecutor(
        ToolRegistry(make_tools(rec)),
        PermissionEngine(PermissionsConfig.model_validate(perms or {})),
        ToolContext(Settings(**settings)),
        confirmer=confirmer,
        audit=AuditLog(db),
    )
    return ex, confirmer, AuditLog(db)


def run(ex: ToolExecutor, name: str, **arguments):
    return asyncio.run(ex.execute(ToolCall(name, arguments)))


# ------------------------------------------------------------ permission engine


def test_permission_decisions() -> None:
    engine = PermissionEngine(PermissionsConfig())
    assert engine.decide("x", 0)[0] == Decision.ALLOW
    assert engine.decide("x", 1)[0] == Decision.CONFIRM
    assert engine.decide("x", 2)[0] == Decision.CONFIRM
    assert engine.decide("x", 3)[0] == Decision.BLOCK
    relaxed = PermissionEngine(PermissionsConfig(confirm_moderate=False, block_critical=False))
    assert relaxed.decide("x", 1)[0] == Decision.ALLOW
    assert relaxed.decide("x", 2)[0] == Decision.CONFIRM  # dangerous always asks
    assert relaxed.decide("x", 3)[0] == Decision.CONFIRM


def test_overrides_can_only_raise() -> None:
    engine = PermissionEngine(PermissionsConfig(tool_levels={"delete_thing": 0, "read_thing": 2}))
    assert engine.effective_level("delete_thing", RiskLevel.DANGEROUS) == RiskLevel.DANGEROUS
    assert engine.effective_level("read_thing", RiskLevel.SAFE) == RiskLevel.DANGEROUS


# --------------------------------------------------------------------- executor


def test_safe_tool_runs_without_confirmation(tmp_path: Path) -> None:
    rec = Recorder()
    ex, confirmer, audit = executor(tmp_path, rec)
    execution = run(ex, "read_thing", path="notes.txt")
    assert execution.result.ok and rec.calls[0].path == "notes.txt" and not confirmer.requests
    assert audit.recent()[0]["event"] == "tool_result"


def test_unknown_tool_and_bad_arguments_never_reach_handler(tmp_path: Path) -> None:
    rec = Recorder()
    ex, *_ = executor(tmp_path, rec)
    assert run(ex, "rm_rf").result.error == "unknown_tool"
    assert run(ex, "read_thing").result.error == "invalid_arguments"
    assert run(ex, "read_thing", path="x" * 100).result.error == "invalid_arguments"
    # the model cannot smuggle in extra fields such as a fake "confirmed" flag
    assert run(ex, "delete_thing", path="a", confirmed=True).result.error == "invalid_arguments"
    bad_json = asyncio.run(ex.execute(ToolCall("read_thing", {}, parse_error="not JSON")))
    assert bad_json.result.error == "invalid_arguments"
    assert rec.calls == []


def test_dangerous_tool_needs_confirmation(tmp_path: Path) -> None:
    rec = Recorder()
    ex, confirmer, audit = executor(tmp_path, rec, confirm=False)
    execution = run(ex, "delete_thing", path="old_project")
    assert execution.result.error == "declined" and rec.calls == []
    assert confirmer.requests[0].summary == "Delete old_project? This may not be recoverable."
    events = [e["event"] for e in audit.recent()]
    assert "confirmation_requested" in events and "confirmation_answered" in events

    ex, confirmer, _ = executor(tmp_path, rec, confirm=True)
    assert run(ex, "delete_thing", path="old_project").result.ok and len(rec.calls) == 1


def test_moderate_follows_setting_and_critical_is_blocked(tmp_path: Path) -> None:
    rec = Recorder()
    ex, confirmer, _ = executor(tmp_path, rec, confirm=True)
    run(ex, "move_thing", path="a")
    assert len(confirmer.requests) == 1
    ex, confirmer, _ = executor(tmp_path, rec, perms={"confirm_moderate": False})
    run(ex, "move_thing", path="a")
    assert confirmer.requests == []
    ex, confirmer, _ = executor(tmp_path, rec, confirm=True)
    assert run(ex, "format_disk", path="C:").result.error == "blocked" and confirmer.requests == []


def test_argument_dependent_risk(tmp_path: Path) -> None:
    rec = Recorder()
    ex, confirmer, _ = executor(tmp_path, rec, confirm=False)
    assert run(ex, "maybe_risky", path="Documents").result.ok
    assert run(ex, "maybe_risky", path="C:/Windows/System32").result.error == "declined"


def test_timeouts_crashes_and_friendly_errors(tmp_path: Path) -> None:
    rec = Recorder()
    ex, _, audit = executor(tmp_path, rec)
    started = time.monotonic()
    assert run(ex, "slow_thing").result.error == "timeout" and time.monotonic() - started < 2
    crashed = run(ex, "crash_thing").result
    assert crashed.error == "internal" and "SECRET" not in crashed.message
    friendly = run(ex, "friendly_fail").result
    assert friendly.error == "not_found" and friendly.message == "Photoshop is not installed on this computer."
    assert "SECRET123456" not in json.dumps(audit.recent())


def test_offline_mode_blocks_network_tools(tmp_path: Path) -> None:
    rec = Recorder()
    ex, *_ = executor(tmp_path, rec, offline_mode=True)
    assert run(ex, "web_thing", path="x").result.error == "offline" and rec.calls == []


def test_audit_redacts_secret_arguments(tmp_path: Path) -> None:
    rec = Recorder()
    ex, _, audit = executor(tmp_path, rec)
    run(ex, "read_thing", path="password=hunter2")
    assert "hunter2" not in json.dumps(audit.recent())


def test_pending_confirmer_round_trip() -> None:
    confirmer = PendingConfirmer()

    async def scenario():
        task = asyncio.create_task(confirmer.confirm(ConfirmationRequest("t", RiskLevel.DANGEROUS, "Restart now?")))
        while confirmer.pending() is None:
            await asyncio.sleep(0.01)
        assert confirmer.pending().summary == "Restart now?"
        confirmer.resolve(True)
        return await task

    assert asyncio.run(scenario()) is True
    assert confirmer.pending() is None


# ----------------------------------------------------------------------- tools


def test_registry_and_schema() -> None:
    registry = build_default_registry()
    specs = {s.name: s for s in registry.specs()}
    assert {"get_datetime", "get_weather", "media_control", "recall_activity", "focus_mode", "close_all_apps", "daily_briefing", "organize_desktop", "empty_recycle_bin", "get_news", "play_music", "search_history", "ask_documents", "index_documents", "forget_history", "calendar_events", "read_emails", "send_email", "click_text", "type_text", "press_keys", "scroll_screen", "save_contact", "list_contacts", "call_contact", "text_contact", "read_messages", "calculate", "open_application", "restart_application", "close_application", "window_control", "open_website",
            "search_in_browser", "system_status", "list_processes", "end_process", "volume_control", "brightness_control", "run_project", "git_commit", "youtube_control",
            "arrange_windows", "read_whatsapp", "undo_last_action", "list_undoable", "current_context", "request_context", "proactive_control", "model_status", "ask_knowledge", "memory_conflicts", "resolve_memory_conflict", "workflow_versions", "restore_workflow", "research", "summarize_video", "interpreter_mode", "study_mode", "improve_yourself", "list_proposals", "show_proposal", "propose_code_change", "test_proposal", "apply_proposal", "reject_proposal", "rollback_proposal", "set_goal", "goal_check", "decision", "preferences", "investigate", "research_brief", "goal_review", "capability_report", "anomalies", "experience", "task_replay", "digital_twin", "build_tool", "workspace", "retrieval_sources", "screen_watch", "gesture_control", "audio_output", "performance_mode", "work_snapshot", "finance_plan", "data_refinery", "wellness", "device_mesh", "skill_card", "dev_session", "guardian", "what_changed", "situations", "attention", "service_impact", "transaction", "vscode", "office_info", "retention", "privacy_zones", "interaction_advice", "radar_feedback", "handoff", "list_goals",
            "update_goal", "start_mission", "resume_mission", "cancel_mission", "mission_status", "mission_history", "rollback_mission", "task_center", "list_agents", "delegate", "run_team", "explain_action", "diagnose_jarvis", "pc_health", "trace_request", "integrations", "search_everything", "experiment", "security_scan", "memory_admin", "voice_stats", "activity_timeline", "health_history", "program_history", "troubleshoot", "background_mode", "simulate_workflow", "mobile_sync", "settings_profile", "google", "routine_suggestions", "ask_history", "clipboard_history", "approvals", "find_duplicates", "batch_rename", "expenses", "phone_control", "journal", "habits", "calendar_conflicts", "local_device", "learning", "tune_ai", "learning_audit", "sensors", "plugin_check", "resources", "verification_coverage", "investigate_problem", "deadline_risk", "schedule_whatif", "app_adapter", "phone_context", "root_cause", "inference_queue", "vault_scan", "a2a_status", "a2a_manage", "location_status", "needs_me", "commitments", "environment_report", "pc_checkup", "manage_places", "camera_session", "analyze_photo", "sound_monitor", "media_search", "media_manage", "inference_providers", "voice_data", "import_bank_sms", "clipboard_ai", "excel_control", "github", "triage_emails", "email_draft", "learn", "research_project", "export_workflow", "import_workflow", "plan_my_day", "repair_jarvis", "explain_memory",
            "manage_memory", "answer_sources", "memory_used", "what_do_you_know", "what_can_you_do", "device_toggle", "screen_recording", "zip_items",
            "extract_zip", "merge_pdfs", "shrink_images", "convert_image", "word_to_pdf", "press_control",
            "project_profile", "add_project_note", "search_code", "code_impact", "read_code", "edit_code", "restore_code", "check_project", "safe_edit",
            "notifications", "set_mode", "emergency_control", "list_devices", "device_info", "add_device", "remove_device",
            "check_device", "wake_device",
            "power_control", "open_path", "list_folder", "find_files", "file_info", "create_item", "rename_item",
            "move_item", "delete_item", "git_status", "git_log", "review_changes", "run_command", "open_in_editor",
            "dev_environment", "remember", "recall", "forget", "forget_everything", "web_search", "browser_open",
            "browser_read", "browser_click", "browser_type", "browser_tabs", "browser_back", "browser_close",
            "set_reminder", "snooze_reminder", "schedule_task", "save_workflow", "teach_workflow", "run_workflow", "list_automations",
            "manage_automation", "learning_audit", "sensors", "plugin_check", "resources", "verification_coverage", "investigate_problem", "deadline_risk", "schedule_whatif", "app_adapter", "phone_context", "root_cause", "inference_queue", "vault_scan", "a2a_status", "a2a_manage", "location_status", "needs_me", "commitments", "environment_report", "pc_checkup", "manage_places", "camera_session", "analyze_photo", "sound_monitor", "media_search", "media_manage", "inference_providers", "take_screenshot", "read_screen", "describe_screen", "write_document", "home_control", "home_status", "read_clipboard", "copy_to_clipboard", "add_todo", "list_todos", "command_center", "complete_todo", "update_todo", "delete_todo", "meeting_notes", "click_thing", "organize_downloads", "open_settings_page", "windows_update", "set_nickname", "remove_nickname", "list_nicknames", "review_mistakes", "accept_fix", "this_window", "app_command", "mouse_control", "jarvis_control", "change_setting", "memory_suggestion", "dictation", "whatsapp_message", "clean_temp_files", "find_my_files", "vscode", "office_info", "retention", "privacy_zones", "interaction_advice", "what_changed", "situations", "attention", "service_impact", "transaction"} == set(specs)
    schema = specs["calculate"].parameters
    assert schema["required"] == ["expression"] and "title" not in schema
    with pytest.raises(ValueError):
        registry.register(registry.get("calculate"))
    with pytest.raises(ValueError):
        ToolRegistry([Tool("Bad Name", "x", lambda a, c: None)])


def test_datetime_tool() -> None:
    result = get_datetime(None, ToolContext(Settings(), clock=lambda: NOW))
    assert result.data["time_24h"] == "13:02" and result.data["weekday"] == "Wednesday"
    assert result.data["time_12h"] == "1:02 PM" and result.data["utc_offset"] == "UTC+06:00"


@pytest.mark.parametrize(
    ("expr", "value"),
    [("2+2", 4), ("(12.5*4)/3", 16.6666666667), ("2^10", 1024), ("sqrt(144)", 12), ("15% of", None),
     ("1,000 * 3", 3000), ("-3 ** 2", -9), ("round(pi, 2)", 3.14)],
)
def test_calculator(expr: str, value) -> None:
    if value is None:
        with pytest.raises(ToolError):
            safe_eval(expr)
    else:
        assert safe_eval(expr) == pytest.approx(value)


@pytest.mark.parametrize(
    "expr",
    ["__import__('os').system('calc')", "().__class__", "open('x')", "9**9**9", "10**1000 * 10**1000",
     "1/0", "lambda: 1", "[1,2]", "x", "sqrt(-1)"],
)
def test_calculator_rejects_unsafe_or_invalid(expr: str) -> None:
    with pytest.raises(ToolError):
        safe_eval(expr)


def test_pending_confirmer_is_never_asked_twice() -> None:
    """Regression: the voice UI polls pending() right after resolve(); the request
    must already be gone, even before the executor's coroutine has resumed."""
    confirmer = PendingConfirmer()
    seen: list[str] = []

    async def scenario():
        task = asyncio.create_task(confirmer.confirm(ConfirmationRequest("power_control", RiskLevel.MODERATE, "Sleep now?")))
        while confirmer.pending() is None:
            await asyncio.sleep(0.01)
        seen.append(confirmer.pending().summary)
        confirmer.resolve(False)
        assert confirmer.pending() is None  # immediately, not after the task resumes
        confirmer.resolve(True)  # a late second answer is ignored
        return await task

    assert asyncio.run(scenario()) is False and seen == ["Sleep now?"]


def test_news_feed_with_entity_tricks_is_refused_not_expanded() -> None:
    from app.tools.housekeeping import parse_rss

    bomb = ('<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]>'
            "<rss><channel><item><title>&b;</title></item></channel></rss>")
    with pytest.raises(ToolError, match="features I don't accept"):
        parse_rss(bomb, 5)
    ok = "<rss><channel><item><title>Hello</title><source>BBC</source></item></channel></rss>"
    assert parse_rss(ok, 5)[0]["title"] == "Hello"
