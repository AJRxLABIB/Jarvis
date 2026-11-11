import pytest

from app.core import referents as R
from app.memory.database import Database


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.initialize()
    return d


def test_extract_labels_from_tool_results():
    assert R.extract({"count": 3, "todos": [{"id": 1, "text": "Fix login"}, {"id": 2, "text": "Write tests"}, {"id": 3, "text": "Deploy"}]}) == ["Fix login", "Write tests", "Deploy"]
    assert R.extract({"results": ["a.txt", "b.txt"]}) == ["a.txt", "b.txt"]
    assert R.extract({"one": [{"text": "only"}]}) == [] and R.extract({"x": [{"id": 1}, {"id": 2}]}) == [] and R.extract(None) == []
    assert R.extract({"big": [f"i{i}" for i in range(50)]}) == []


@pytest.mark.parametrize("text,pos", [("start with the first one", 1), ("open the second file", 2), ("do the last one", -1), ("number 3 please", 3),
                                      ("task 2", 2), ("প্রথমটা শুরু করো", 1), ("the 3rd option", 3), ("what is the first step in cooking rice", None),
                                      ("remind me at ten", None), ("hello", None), ("last night I slept late", None)])
def test_position(text, pos):
    assert R.position(text) == pos


def test_hint_resolves_and_never_guesses():
    t = {"v": 0.0}
    st = R.Referents(clock=lambda: t["v"])
    assert st.capture("list_todos", {"todos": [{"text": "Fix login"}, {"text": "Write tests"}, {"text": "Deploy"}]})
    assert st.capture("set_volume", {"level": 30}) is False            # a result without a list keeps the list in place
    h = R.hint("start with the first one", st.current())
    assert '"Fix login"' in h and "item 1 of 3" in h and "list_todos" in h
    assert '"Deploy"' in R.hint("do the last one", st.current())
    assert "only 3 items" in R.hint("open number 7", st.current())
    assert R.hint("what's the weather", st.current()) == ""
    t["v"] = R.TTL_S + 1
    assert st.current() is None and R.hint("the first one", st.current()) == ""   # an old list is not reused


def test_executor_captures_lists_and_conversation_uses_them():
    import inspect

    from app.agent import conversation, tool_manager

    assert "referents.STORE.capture" in inspect.getsource(tool_manager.ToolExecutor.execute)
    assert "referents.hint" in inspect.getsource(conversation)


def test_new_agents_are_bounded():
    from app.core.agents import AGENTS, KNOWN_CATEGORIES
    from app.security.permissions import RiskLevel

    for name in ("guardian", "monitor", "phone", "automator"):
        a = AGENTS[name]
        assert a.categories <= set(KNOWN_CATEGORIES), name
    assert AGENTS["guardian"].max_risk == RiskLevel.SAFE and AGENTS["monitor"].max_risk == RiskLevel.SAFE and AGENTS["automator"].max_risk == RiskLevel.SAFE
    assert AGENTS["guardian"].allows("change_setting", "system", RiskLevel.SAFE)[0] is False
    assert AGENTS["guardian"].allows("security_scan", "system", RiskLevel.MODERATE)[0] is False       # read-only: nothing that changes the PC
    assert AGENTS["phone"].allows("text_contact", "phone", RiskLevel.MODERATE)[0] is False
    assert AGENTS["automator"].allows("run_workflow", "automation", RiskLevel.SAFE)[0] is False
    assert AGENTS["monitor"].allows("pc_health", "system", RiskLevel.SAFE)[0] is True


def test_notification_search(db):
    from app.core.notifications import NotificationCenter
    from app.tools import proactive_tools as PT
    from app.tools.base import ToolError

    c = NotificationCenter(db)
    c.add("Mission paused", "Backup stopped at step 2", source="jarvis", category="mission")
    c.add("New device", "Unknown phone tried to pair", source="jarvis", category="security", priority="high")
    c.add("Backup finished", "", source="jarvis", category="system")
    c.mark_read()
    c.archive()
    assert [n.title for n in c.search("backup")] == ["Backup finished", "Mission paused"]      # history: read and archived are still found
    assert [n.title for n in c.search("backup", category="mission")] == ["Mission paused"]
    assert c.search("") == [] and c.search("zzz") == []
    ctx = type("C", (), {"database": db})()
    out = PT.notifications_tool(PT.NoticeArgs(action="search", query="pair"), ctx)
    assert "New device" in out.message and "security scan" in out.message
    with pytest.raises(ToolError):
        PT.notifications_tool(PT.NoticeArgs(action="search"), ctx)
