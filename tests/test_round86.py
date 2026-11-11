from app.core import circuit as C, file_attribution as FA
from app.core.taskstate import display_state


# ---- circuit breakers ---------------------------------------------------------------------------------------------------
def test_breaker_opens_after_transient_failures_and_half_opens():
    t = {"v": 0.0}
    b = C.Breakers(clock=lambda: t["v"])
    for _ in range(2):
        b.record("web_search", False, transient=True, error="timed out")
    assert b.allow("web_search") == (True, "")                        # not yet
    b.record("web_search", False, transient=True, error="timed out")
    ok, why = b.allow("web_search")
    assert not ok and "paused" in why and "3 times" in why
    assert b.allow("web_search", direct=True)[0]                      # your own command is always tried
    b.record("web_search", False, transient=True, error="timed out")  # that trial failed: open again, longer
    t["v"] += 61
    assert b.allow("web_search")[0] is False
    t["v"] += 60
    assert b.allow("web_search")[0] and b.allow("web_search")[0] is False   # one trial at a time
    b.record("web_search", True)
    assert b.allow("web_search") == (True, "") and b.snapshot() == []


def test_breaker_ignores_wrong_requests_and_is_per_tool():
    b = C.Breakers(clock=lambda: 0.0)
    for _ in range(10):
        b.record("github", False, transient=False, error="not found")
    assert b.allow("github")[0] and "No service is paused" in C.describe([r for r in b.snapshot() if r["failures"]])
    for _ in range(3):
        b.record("github", False, transient=True, error="connection reset")
    assert not b.allow("github")[0] and b.allow("web_search")[0]
    assert "PAUSED" in C.describe(b.snapshot())
    assert b.reset("github") == 1 and b.allow("github")[0]


def test_cooldown_is_capped():
    t = {"v": 0.0}
    b = C.Breakers(clock=lambda: t["v"])
    for _ in range(40):
        b.record("x", False, transient=True)
    assert b.snapshot()[0]["paused_s"] <= C.MAX_COOLDOWN


def test_executor_uses_breaker_only_for_network_tools():
    from app.agent.tool_manager import ToolExecutor
    from app.tools.base import ToolResult

    C.BREAKERS.reset()
    ex = ToolExecutor.__new__(ToolExecutor)
    net = type("T", (), {"name": "web_search_x", "needs_network": True})()
    local = type("T", (), {"name": "read_file_x", "needs_network": False})()
    for _ in range(3):
        ex._breaker_record(net, ToolResult.failure("timeout", "timed out"))
        ex._breaker_record(local, ToolResult.failure("timeout", "timed out"))
    blocked = ex._breaker_blocks(net, False)
    assert blocked is not None and blocked.error == "service_paused"
    assert ex._breaker_blocks(net, True) is None and ex._breaker_blocks(local, False) is None
    ex._breaker_record(net, ToolResult.success("ok"))
    assert ex._breaker_blocks(net, False) is None
    for _ in range(5):
        ex._breaker_record(net, ToolResult.failure("invalid_arguments", "bad query"))
    assert ex._breaker_blocks(net, False) is None                     # a wrong request never pauses a service
    C.BREAKERS.reset()


# ---- file attribution ---------------------------------------------------------------------------------------------------
AUDIT = """<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System><EventID>4663</EventID><TimeCreated SystemTime='2026-11-11T10:00:00.123Z'/></System>
<EventData><Data Name='ObjectType'>File</Data><Data Name='ObjectName'>D:\\Important\\report.docx</Data><Data Name='ProcessName'>C:\\Program Files\\App\\winword.exe</Data><Data Name='AccessMask'>0x2</Data></EventData></Event>
<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System><EventID>4663</EventID><TimeCreated SystemTime='2026-11-11T10:05:00.000Z'/></System>
<EventData><Data Name='ObjectType'>File</Data><Data Name='ObjectName'>D:\\Important\\old.txt</Data><Data Name='ProcessName'>C:\\Tools\\cleaner.exe</Data><Data Name='AccessMask'>0x10000</Data></EventData></Event>
<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System><EventID>4663</EventID><TimeCreated SystemTime='2026-11-11T10:06:00.000Z'/></System>
<EventData><Data Name='ObjectType'>File</Data><Data Name='ObjectName'>D:\\Important\\read.txt</Data><Data Name='ProcessName'>C:\\x\\reader.exe</Data><Data Name='AccessMask'>0x1</Data></EventData></Event>"""
SYSMON = """<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System><EventID>11</EventID><TimeCreated SystemTime='2026-11-11T11:00:00.000Z'/></System>
<EventData><Data Name='Image'>C:\\Users\\me\\AppData\\odd.exe</Data><Data Name='TargetFilename'>D:\\Other\\new.bin</Data></EventData></Event>"""


def test_parse_audit_and_sysmon_events():
    a = FA.parse(AUDIT)
    assert [(c.program, c.action) for c in a] == [("winword.exe", "written"), ("cleaner.exe", "deleted")]      # the read-only access is not a change
    s = FA.parse(SYSMON)
    assert s == [FA.Change("2026-11-11T11:00:00", "odd.exe", "D:\\Other\\new.bin", "created", "sysmon")]
    assert FA.parse("") == [] and FA.parse("<not xml") == []


def test_summarise_filters_by_folder_without_prefix_tricks():
    changes = FA.parse(AUDIT) + FA.parse(SYSMON)
    text = FA.summarise(changes, "D:\\Important")
    assert "2 recorded change(s)" in text and "cleaner.exe: 1 deleted" in text and "odd.exe" not in text
    assert FA.under("D:\\Important2\\x.txt", "D:\\Important") is False and FA.under("d:/important/a/b.txt", "D:\\Important")
    assert "No recorded file change" in FA.summarise(changes, "E:\\Nothing")


def test_read_reports_missing_sources_instead_of_silence():
    scripts = {"file_audit_events": "A", "sysmon_file_events": "S"}

    def run(script):
        if script == "A":
            raise RuntimeError("Attempted to perform an unauthorized operation.")
        raise RuntimeError("No events were found that match the specified selection criteria.")

    changes, notes = FA.read(run, scripts)
    assert changes == [] and "administrator" in notes[0] and "not set up" in notes[1]
    text = FA.describe(changes, notes)
    assert "no record" in text and "setup" in text
    changes, notes = FA.read(lambda s: AUDIT if s == "A" else "", scripts)
    assert len(changes) == 2 and "Sysmon" in notes[0] and "winword.exe" in FA.describe(changes, notes, "D:\\Important")


def test_scripts_are_fixed_and_read_only():
    from app.core import pc_checkup

    for key in ("file_audit_events", "sysmon_file_events"):
        s = pc_checkup.EVENT_SCRIPTS[key]
        assert s.startswith("(Get-WinEvent") and not any(w in s.lower() for w in ("set-", "remove-", "clear-", "auditpol", "invoke-"))
    assert "auditpol" in FA.SETUP and "ADMINISTRATOR" in FA.SETUP
    assert set(pc_checkup.SCRIPTS) == set(pc_checkup.RULES)            # the check-up itself is unchanged


# ---- mission display states ---------------------------------------------------------------------------------------------
def test_display_state_explains_paused():
    assert display_state("paused", "Checkpoint before step 3 (deploy): waiting for your go-ahead") == "awaiting approval"
    assert display_state("paused", "Paused at step 2 (build): compile error", ["done", "failed"]) == "blocked"
    assert display_state("paused", "interrupted (Jarvis restarted)").startswith("interrupted")
    assert display_state("paused", "stopped by you") == "paused"
    assert display_state("planned") == "ready" and display_state("completed") == "completed" and display_state("running") == "running"


def test_new_tools_registered():
    from app.tools.catalog import build_default_registry

    reg = build_default_registry()
    assert reg.get("service_breakers") is not None and reg.get("file_attribution") is not None
