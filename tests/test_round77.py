"""Round 77: IMAP mail reader (read-only), commitments ledger, "needs me" ranking, environment report, PC check-up. Fakes only: no mailbox, network or Windows."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import commitments as C, environment as ENV, mail_imap as M, needs_me as NM, pc_checkup as PC

NOW = datetime(2026, 11, 10, 10, 0)  # a Tuesday
TODAY = NOW.date()


def run(coro):
    return asyncio.run(coro)


def mkdb(tmp_path):
    from app.memory.database import Database

    db = Database(tmp_path / "m.db")
    db.initialize()
    return db


def mail(sender="Rahim <rahim@example.com>", subject="Report", body="Please send the report by Friday.", to="labib@example.com", date_="Tue, 10 Nov 2026 09:00:00 +0600", html=None, attach=None):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Date"], m["Message-ID"] = sender, to, subject, date_, f"<{abs(hash((sender, subject, body)))}@x>"
    m.set_content(body)
    if html:
        m.add_alternative(html, subtype="html")
    if attach:
        m.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename=attach)
    return m.as_bytes()


# ------------------------------------------------------------------ IMAP


def test_a_message_is_parsed_with_decoded_names_quoted_text_cut_and_attachments_only_named():
    raw = mail(sender="=?utf-8?b?4Kaw4Ka54Ka/4Kau?= <rahim@example.com>", body="Hi,\nPlease send the report by Friday.\n\nOn Mon, 9 Nov 2026 Labib wrote:\n> the old text\n", attach="invoice.pdf")
    d = M.parse_message(raw, "7")
    assert d["from"] == "রহিম" and d["address"] == "rahim@example.com" and d["subject"] == "Report" and d["uid"] == "7"
    assert d["body"] == "Hi,\nPlease send the report by Friday." and "old text" not in d["body"]
    assert d["attachments"] == ["invoice.pdf"] and d["received"].startswith("2026-11-10") and d["timestamp"] > 0


def test_html_only_mail_becomes_text_and_broken_mail_never_raises():
    m = EmailMessage()
    m["From"], m["Subject"] = "shop@example.com", "Sale"
    m.set_content("<p>Big <b>sale</b></p><script>steal()</script><style>p{}</style>", subtype="html")
    d = M.parse_message(m.as_bytes())
    assert "Big sale" in " ".join(d["body"].split()) and "steal" not in d["body"] and "<" not in d["body"]
    junk = M.parse_message(b"\xff\xfe not a mail at all")
    assert junk["subject"] == "(no subject)" and junk["from"] == "" and junk["timestamp"] == 0.0


def test_provider_guess_and_sent_folder_detection():
    assert M.guess_host("me@gmail.com") == "imap.gmail.com" and M.guess_host("me@company.example") == "" and M.guess_host("nonsense") == ""
    rows = [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"', b'(\\Trash) "/" "Bin"']
    assert M.sent_folder(rows) == "[Gmail]/Sent Mail"
    assert M.sent_folder([b'(\\HasNoChildren) "." "INBOX"', b'(\\HasNoChildren) "." "Sent Items"']) == "Sent Items"  # no flag: a well-known name
    assert M.sent_folder([b'(\\HasNoChildren) "/" "INBOX"']) == ""
    settings = SimpleNamespace(imap_host="", imap_user="me@gmail.com", imap_password="app pass", imap_port=993, imap_folder="INBOX")
    assert M.Account.from_settings(settings).host == "imap.gmail.com"
    assert M.Account.from_settings(SimpleNamespace(imap_host="", imap_user="", imap_password="")) is None  # not set up


class FakeBox:
    def __init__(self, messages, login_ok=True, sent=None):
        self.messages, self.login_ok, self.sent, self.calls, self.current = messages, login_ok, sent or [], [], None

    def login(self, user, password):
        self.calls.append(("login", user))
        if not self.login_ok:
            raise RuntimeError("AUTHENTICATIONFAILED")
        return "OK", [b""]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\Sent) "/" "Sent"']

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        self.current = self.sent if "Sent" in folder else self.messages
        return "OK", [b"1"]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "search":
            return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.current)))]
        return "OK", [(b"1 (BODY[HEADER] {10}", self.current[int(args[0]) - 1]), b")"]

    def logout(self):
        self.calls.append(("logout",))


def test_fetch_is_read_only_newest_first_capped_and_always_logs_out():
    box = FakeBox([mail(subject=f"m{i}") for i in range(5)])
    acct = M.Account("imap.example.com", "me@example.com", "pw")
    got = M.fetch(acct, 3, True, connect=lambda a: box)
    assert [g["subject"] for g in got] == ["m4", "m3", "m2"]
    assert ("select", '"INBOX"', True) in box.calls  # opened read-only
    fetches = [c for c in box.calls if c[0] == "fetch"]
    assert fetches and all("BODY.PEEK" in c[2] and "BODY[" not in c[2].replace("BODY.PEEK[", "") for c in fetches)  # PEEK: never marks mail as read
    assert ("search", None, "UNSEEN") in box.calls and box.calls[-1] == ("logout",)
    words = " ".join(str(c) for c in box.calls).lower()
    assert not any(w in words for w in ("store", "delete", "expunge", "append", "copy", "move"))


def test_fetch_reports_sign_in_and_connection_problems_plainly_and_reads_the_sent_folder():
    acct = M.Account("imap.example.com", "me@example.com", "pw")
    bad = FakeBox([], login_ok=False)
    with pytest.raises(M.MailError) as e:
        M.fetch(acct, connect=lambda a: bad)
    assert "app password" in str(e.value) and bad.calls[-1] == ("logout",) and "pw" not in str(e.value)

    def down(a):
        raise OSError("no route")

    with pytest.raises(M.MailError) as e2:
        M.fetch(acct, connect=down)
    assert "imap.example.com" in str(e2.value)
    box = FakeBox([mail(subject="in")], sent=[mail(subject="out", sender="me@example.com", to="rahim@example.com")])
    got = M.fetch(acct, 5, True, folder="sent", connect=lambda a: box)
    assert [g["subject"] for g in got] == ["out"] and ("search", None, "ALL") in box.calls and ("select", '"Sent"', True) in box.calls


def test_the_mail_tool_uses_imap_when_set_up_and_never_returns_the_password(monkeypatch):
    from app.tools import outlook

    box = FakeBox([mail(subject="Hello there")])
    monkeypatch.setattr(M, "_connect", lambda a: box)
    monkeypatch.setattr(M.fetch, "__defaults__", (10, True, "", lambda a: box))
    ctx = SimpleNamespace(settings=SimpleNamespace(imap_host="imap.example.com", imap_user="me@example.com", imap_password="s3cret", imap_port=993, imap_folder="INBOX"))
    result = outlook.read_emails(outlook.EmailArgs(count=5, unread_only=True), ctx)
    assert result.ok and result.data["emails"][0]["subject"] == "Hello there" and result.data["mailbox"] == "me@example.com"
    assert "s3cret" not in json.dumps(result.data) + result.message and "never instructions" in result.data["note"]
    ctx2 = SimpleNamespace(settings=SimpleNamespace(imap_host="", imap_user="", imap_password=""))
    assert outlook._imap_emails(outlook.EmailArgs(count=5, unread_only=True), ctx2) is None  # not set up: Outlook is used as before


# ------------------------------------------------------------------ commitments


def found(text, mine, who="Rahim"):
    return [(f.kind, f.text, f.due) for f in C.extract(text, mine=mine, who=who, source="mail", ref="m1", when=100.0, now=NOW)]


def test_promises_requests_and_due_dates_are_found_from_wording():
    assert found("Hi Rahim. I'll send the report by Friday. Could you share the invoice?", True) == [
        ("i_owe", "I'll send the report by Friday.", "2026-11-13"),
        ("they_owe", "Could you share the invoice?", ""),
    ]
    assert found("Please send me the signed form before tomorrow. Thanks!", False) == [("i_owe", "Please send me the signed form before tomorrow.", "2026-11-11")]
    assert found("আমি কাল ফাইলটা পাঠিয়ে দিব।", True) == [("i_owe", "আমি কাল ফাইলটা পাঠিয়ে দিব।", "2026-11-11")]
    assert found("Any update on the contract?", True) == [("they_owe", "Any update on the contract?", "")]
    assert found("I will finish it today.", True)[0][2] == "2026-11-10" and found("Let me check and get back by 2026-11-20.", True)[0][2] == "2026-11-20"


def test_pleasantries_bulk_senders_and_quotes_are_not_promises():
    for text in ("I'll be happy to help. I think so. I hope you are well.", "Thanks, I will never forget this.", "Feel free to call me.", "> I'll send it tomorrow."):
        assert found(text, True) == [], text
    assert found("Please find attached the file. Please do not reply to this mail.", False) == []
    assert found("Please confirm your subscription today.", False, who="newsletter@shop.example") == []
    assert found("Please send the report.", False, who="") == []
    assert len(found(" ".join(f"I will do task {i} tomorrow." for i in range(20)), True)) == 5  # a long mail is capped


def test_due_date_names_only_what_the_sentence_says():
    assert C.due_date("send it by friday", NOW) == "2026-11-13" and C.due_date("on tuesday", NOW) == "2026-11-17"  # "Tuesday" said on a Tuesday = next week
    assert C.due_date("by end of the month", NOW) == "2026-11-30" and C.due_date("next week please", NOW) == "2026-11-16"
    assert C.due_date("by end of week", NOW) == "2026-11-13" and C.due_date("eod", NOW) == "2026-11-10"
    assert C.due_date("sometime soon", NOW) == "" and C.due_date("by 2026-13-45", NOW) == ""


def test_ledger_keeps_dismissed_items_dismissed_and_marks_answered_only_as_a_hint(tmp_path):
    ledger = C.Ledger(mkdb(tmp_path))
    items = C.extract("I'll send the report by Friday. Could you share the invoice?", mine=True, who="Rahim", source="mail", ref="m1", when=100.0, now=NOW)
    assert ledger.add(items) == 2 and ledger.add(items) == 0  # the same sentence from the same message is one item
    promise = next(i for i in ledger.items() if i["kind"] == "i_owe")
    assert ledger.set_status(promise["id"][:6], "dismissed") and ledger.add(items) == 0  # dismissed never comes back
    assert [i["kind"] for i in ledger.items()] == ["they_owe"]
    assert ledger.settle_from([{"who": "rahim", "mine": False, "when": 50.0}]) == 0  # an EARLIER message settles nothing
    assert ledger.settle_from([{"who": "Someone Else", "mine": False, "when": 200.0}]) == 0
    assert ledger.settle_from([{"who": "Rahim", "mine": False, "when": 200.0}]) == 1  # Rahim wrote again: probably answered
    item = ledger.items()[0]
    assert item["status"] == "answered" and C.bucket(item, TODAY) == "answered"  # still listed, to be confirmed
    assert not ledger.set_status("zzzz", "done")
    with pytest.raises(ValueError):
        ledger.set_status(item["id"], "deleted")
    assert ledger.set_status(item["id"], "done") and ledger.items() == []
    assert ledger.prune(keep_days=0, now=10_000.0) == 2


def test_buckets_and_the_description_put_overdue_first_and_quote_the_source(tmp_path):
    ledger = C.Ledger(mkdb(tmp_path))
    mk = lambda kind, who, text, due, t: C.Found(kind, who, text, due, "mail", f"r{t}", who.lower(), float(t))
    ledger.add(
        [
            mk("i_owe", "Rahim", "I'll send the report.", "2026-11-08", 1),
            mk("i_owe", "Karim", "I'll call you.", "2026-11-10", 2),
            mk("i_owe", "Sara", "I'll review it.", "2026-11-12", 3),
            mk("i_owe", "Tina", "I'll think about it.", "", 4),
            mk("they_owe", "Bank", "Could you send the statement?", "2026-11-01", 5),
        ]
    )
    items = ledger.items()
    assert [C.bucket(i, TODAY) for i in sorted(items, key=lambda i: i["created"])] == ["overdue", "today", "soon", "undated", "overdue"]
    text = C.describe(items, TODAY)
    assert text.index("OVERDUE") < text.index("due today") < text.index("due soon") < text.index("no date said")
    assert "You owe (4)" in text and "You are waiting for (1)" in text and '"I\'ll send the report."' in text and "check the quote" in text
    assert "Nothing is open" in C.describe([], TODAY)


def test_migration_43_creates_the_commitments_table(tmp_path):
    from app.memory.database import LATEST_SCHEMA_VERSION

    db = mkdb(tmp_path)
    with db.connect() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(commitments)")}
    assert LATEST_SCHEMA_VERSION >= 43 and {"id", "kind", "who", "text", "due", "status", "thread", "settled"} <= cols


# ------------------------------------------------------------------ needs me


def test_ranking_order_reasons_duplicates_and_quiet_counts():
    triage = {
        "items": [
            {"category": "urgent", "from": "Boss", "subject": "Deadline today", "reasons": ["says 'deadline'"]},
            {"category": "suspicious", "from": "PayPa1", "subject": "Verify your account", "warnings": ["link goes to a different site"]},
            {"category": "routine", "from": "Friend", "subject": "Hi"},
        ]
    }
    notes = [
        SimpleNamespace(label="critical", app="Bank", title="Card blocked", reasons=["bank alert"], duplicate=False, deadline=""),
        SimpleNamespace(label="action", app="Courier", title="Delivery today", reasons=[], duplicate=False, deadline="by 5 pm"),
        SimpleNamespace(label="action", app="Courier", title="Delivery today", reasons=[], duplicate=True, deadline=""),
        SimpleNamespace(label="promo", app="Shop", title="Sale", reasons=[], duplicate=False, deadline=""),
    ]
    promises = [
        {"kind": "i_owe", "who": "Rahim", "text": "I'll send the report.", "due": "2026-11-08", "status": "open"},
        {"kind": "they_owe", "who": "Bank", "text": "Could you send the statement?", "due": "2026-11-01", "status": "open"},
        {"kind": "i_owe", "who": "Tina", "text": "I'll think about it.", "due": "", "status": "open"},
    ]
    todos = [{"text": "Pay rent", "due": "2026-11-09"}, {"text": "Buy milk", "due": "2026-11-10"}, {"text": "Someday", "due": None}]
    items = NM.rank([NM.from_mail(triage), NM.from_whatsapp([{"name": "Ammu", "unread": 2}]), NM.from_notifications(notes), NM.from_commitments(promises, TODAY), NM.from_todos(todos, TODAY)])
    assert [(i.score, i.source) for i in items] == [
        (100, "mail"),
        (90, "promise"),
        (85, "notification"),
        (80, "mail"),
        (75, "to-do"),
        (60, "notification"),
        (60, "whatsapp"),
        (55, "promise"),
        (40, "to-do"),
    ]
    assert "do not click" in items[0].why and "promised this for 2026-11-08" in items[1].why
    text = NM.describe(items, ["mail", "promises"], {"WhatsApp": "WhatsApp isn't open"}, {"routine mail": 1, "promotional mail": 0})
    assert "9 thing(s) need you" in text and "NOT checked: WhatsApp (WhatsApp isn't open)" in text and "Not listed: 1 routine mail" in text
    quiet = NM.describe([], ["mail"], {}, {})
    assert "Nothing needs you right now in mail" in quiet and "NOT checked" not in quiet
    assert NM.rank([[NM.Item(60, "whatsapp", "Ammu", "x")], [NM.Item(60, "whatsapp", "ammu", "y")]]) == [NM.Item(60, "whatsapp", "Ammu", "x")]


def test_needs_me_tool_names_sources_it_could_not_read(tmp_path, monkeypatch):
    from app.tools import daily_tools as T, messages, outlook
    from app.tools.base import ToolError

    def no_outlook(*a, **k):
        raise ToolError("Outlook is not installed", "outlook_unavailable")

    def no_whatsapp(*a, **k):
        raise ToolError("WhatsApp isn't open", "not_running")

    monkeypatch.setattr(outlook, "read_emails", no_outlook)  # never touch a real Outlook / WhatsApp on the machine running the tests
    monkeypatch.setattr(messages, "read_whatsapp", no_whatsapp)

    db = mkdb(tmp_path)
    C.Ledger(db).add([C.Found("i_owe", "Rahim", "I'll send the report.", "2020-01-01", "mail", "r1", "rahim", 1.0)])
    ctx = SimpleNamespace(database=db, settings=SimpleNamespace(imap_host="", imap_user="", imap_password=""), paths=None)
    result = run(T.needs_me(T.NoArgs(), ctx))
    assert result.ok and "Rahim" in result.message and "promises" in result.data["checked"]
    assert "mail" in result.data["not_checked"] and "WhatsApp" in result.data["not_checked"] and "NOT checked" in result.message  # no Outlook / WhatsApp here: said so


# ------------------------------------------------------------------ environment


def test_aqi_and_uv_bands_use_the_published_limits():
    assert [ENV.aqi_band(v)[0] for v in (0, 50, 51, 100, 101, 150, 151, 200, 201, 300, 301, 900)] == [
        "good",
        "good",
        "moderate",
        "moderate",
        "unhealthy for sensitive groups",
        "unhealthy for sensitive groups",
        "unhealthy",
        "unhealthy",
        "very unhealthy",
        "very unhealthy",
        "hazardous",
        "hazardous",
    ]
    assert [ENV.uv_band(v)[0] for v in (0, 2, 3, 5, 6, 7, 8, 10, 11, 14)] == ["low", "low", "moderate", "moderate", "high", "high", "very high", "very high", "extreme", "extreme"]
    assert ENV.aqi_band(None) == ("unknown", "") and ENV.uv_band(None) == ("unknown", "") and ENV.aqi_band(-1)[0] == "unknown"
    assert "N95" in ENV.aqi_band(180)[1] and ENV.aqi_band(30)[1] == "Air quality is good."


def test_rain_ahead_looks_only_at_the_next_hours():
    times = [f"2026-11-10T{h:02d}:00" for h in range(8, 20)]
    chances = [0, 0, 10, 20, 30, 60, 80, 40, 10, 90, 90, 90]  # 13:00 = 60, 14:00 = 80; 17:00+ is beyond 6 hours
    assert ENV.rain_ahead(times, chances, NOW) == (80, "13:00")
    assert ENV.rain_ahead(times, [5] * 12, NOW) == (5, "") and ENV.rain_ahead([], [], NOW) == (None, "")
    assert ENV.rain_ahead(["nonsense", "2026-11-10T11:00"], [99, None], NOW) == (None, "")


def test_report_gives_advice_only_when_warranted_and_names_what_is_missing():
    air = {"current": {"us_aqi": 172, "pm2_5": 96.4, "pm10": 140.2}}
    forecast = {
        "current": {"apparent_temperature": 37.2},
        "daily": {"uv_index_max": [8.4], "sunrise": ["2026-11-10T06:08"], "sunset": ["2026-11-10T17:14"]},
        "hourly": {"time": ["2026-11-10T12:00", "2026-11-10T13:00"], "precipitation_probability": [20, 70]},
    }
    r = ENV.build("Dhaka", air, forecast, NOW)
    text = r.format()
    assert "AQI 172 - unhealthy" in text and "PM2.5 96" in text and "UV today: up to 8 - very high" in text and "06:08 to 17:14" in text
    assert "N95" in text and "umbrella" in text and "13:00" in text and "Very hot" in text and "regional model" in text
    clean = ENV.build("Sylhet", {"current": {"us_aqi": 30}}, {"current": {"apparent_temperature": 24}, "daily": {"uv_index_max": [1.0]}, "hourly": {}}, NOW)
    assert clean.advice() == [] and "What to do" not in clean.format()
    partial = ENV.build("Dhaka", None, forecast, NOW)
    assert partial.aqi is None and "Not available right now: air quality" in partial.format() and "AQI" not in partial.format().split("Not available")[0]
    assert ENV.build("Dhaka", air, None, NOW).missing == ["weather"]
    assert ENV.build("X", {"current": {"us_aqi": True}}, {}, NOW).aqi is None  # a wrong type is not a reading


def test_environment_tool_survives_one_service_failing(monkeypatch):
    from app.tools import assistant as A, daily_tools as T
    from app.tools.base import ToolError

    async def geocode(place):
        return "Dhaka", 23.8, 90.4

    async def get_json(url, params):
        if "air-quality" in url:
            raise ToolError("down", "service_error")
        return {"current": {"apparent_temperature": 30}, "daily": {"uv_index_max": [6.0], "sunrise": ["2026-11-10T06:08"], "sunset": ["2026-11-10T17:14"]}, "hourly": {}}

    monkeypatch.setattr(A, "geocode", geocode)
    monkeypatch.setattr(A, "_get_json", get_json)
    ctx = SimpleNamespace(settings=SimpleNamespace(home_location="Dhaka"))
    result = run(T.environment_report(T.EnvArgs(), ctx))
    assert result.ok and result.data["aqi"] is None and result.data["aqi_band"] == "unknown" and "Not available right now: air quality" in result.message

    async def all_down(url, params):
        raise ToolError("down", "service_error")

    monkeypatch.setattr(A, "_get_json", all_down)
    with pytest.raises(ToolError):
        run(T.environment_report(T.EnvArgs(), ctx))
    with pytest.raises(ToolError):
        run(T.environment_report(T.EnvArgs(), SimpleNamespace(settings=SimpleNamespace(home_location=""))))


# ------------------------------------------------------------------ PC check-up

GOOD = {
    "defender": {"AMServiceEnabled": True, "AntivirusEnabled": True, "RealTimeProtectionEnabled": True, "AntivirusSignatureAge": 1, "QuickScanAge": 3},
    "firewall": [{"Name": "Domain", "Enabled": True}, {"Name": "Private", "Enabled": True}, {"Name": "Public", "Enabled": True}],
    "disks": {"Name": "NVMe X", "Media": "SSD", "Health": "Healthy", "SizeGB": 512, "Wear": 12, "TempC": 41, "ReadErrors": 0},
    "battery": {"Design": 60000, "Full": 55000, "Cycles": 120},
    "restart": {"CBS": False, "WU": False},
    "updates": {"Last": "2026-11-01", "Days": 9},
    "startup": [{"Name": "a", "Location": "x"}],
    "volumes": [{"DriveLetter": "C", "SizeGB": 475.0, "FreeGB": 200.0}],
}
KEY_OF = {script: key for key, script in PC.SCRIPTS.items()}


def runner(data):
    return lambda script: json.dumps(data[KEY_OF[script]])


def status(key, value):
    return PC.RULES[key](value).status


def test_a_healthy_pc_is_all_ok_and_every_script_is_read_only():
    checks = PC.run(runner(GOOD))
    assert [c.status for c in checks] == [PC.OK] * 8 and "looks healthy" in PC.describe(checks)
    for script in PC.SCRIPTS.values():
        low = script.lower()
        assert not any(verb in low for verb in ("set-", "remove-", "stop-", "start-", "new-", "disable-", "enable-", "invoke-", "restart-", "uninstall", "format-volume", "clear-", "iex"))
        assert "convertto-json" in low
    assert set(PC.SCRIPTS) == set(PC.RULES) == set(PC.TITLES)


def test_each_rule_reports_problem_attention_and_unknown_at_its_threshold():
    assert status("defender", {**GOOD["defender"], "RealTimeProtectionEnabled": False}) == PC.PROBLEM
    assert status("defender", {**GOOD["defender"], "AntivirusSignatureAge": 9}) == PC.ATTENTION and status("defender", {**GOOD["defender"], "QuickScanAge": 45}) == PC.ATTENTION
    assert status("defender", {**GOOD["defender"], "QuickScanAge": 4294967295}) == PC.OK and status("defender", {**GOOD["defender"], "AntivirusEnabled": False}) == PC.ATTENTION
    assert status("defender", None) == PC.UNKNOWN
    assert status("firewall", [{"Name": "Public", "Enabled": False}]) == PC.PROBLEM and status("firewall", {"Name": "Public", "Enabled": 1}) == PC.OK and status("firewall", []) == PC.UNKNOWN
    assert status("disks", {**GOOD["disks"], "Health": "Warning"}) == PC.PROBLEM and status("disks", {**GOOD["disks"], "ReadErrors": 3}) == PC.PROBLEM
    assert status("disks", {**GOOD["disks"], "Wear": 85}) == PC.ATTENTION and status("disks", {**GOOD["disks"], "TempC": 70}) == PC.ATTENTION and status("disks", None) == PC.UNKNOWN
    no_admin = PC.check_disks({"Name": "D", "Health": "Healthy", "Wear": None, "TempC": None})
    assert no_admin.status == PC.OK and "administrator" in no_admin.summary
    assert status("battery", {"Design": 60000, "Full": 45000}) == PC.ATTENTION and status("battery", {"Design": 60000, "Full": 30000}) == PC.PROBLEM
    assert status("battery", {"Design": None, "Full": None}) == PC.UNKNOWN and "75%" in PC.check_battery({"Design": 60000, "Full": 45000, "Cycles": 300}).summary
    assert PC.check_battery({"Design": 50000, "Full": 52000}).summary.startswith("Holds 100%")
    assert status("restart", {"CBS": True, "WU": False}) == PC.ATTENTION and status("restart", None) == PC.UNKNOWN
    assert (
        status("updates", {"Last": "2026-10-01", "Days": 40}) == PC.ATTENTION
        and status("updates", {"Last": "2026-08-01", "Days": 101}) == PC.PROBLEM
        and status("updates", {"Days": None}) == PC.UNKNOWN
    )
    assert status("startup", [{"Name": f"p{i}"} for i in range(20)]) == PC.ATTENTION and status("startup", []) == PC.OK and status("startup", None) == PC.UNKNOWN
    assert status("volumes", [{"DriveLetter": "C", "SizeGB": 475, "FreeGB": 10}]) == PC.PROBLEM and status("volumes", [{"DriveLetter": "C", "SizeGB": 475, "FreeGB": 50}]) == PC.ATTENTION
    assert status("volumes", []) == PC.UNKNOWN


def test_a_failing_or_unreadable_reading_is_unknown_and_never_stops_the_others():
    def flaky(script):
        key = KEY_OF[script]
        if key == "defender":
            raise RuntimeError("Access is denied")
        if key == "firewall":
            return "this is not json"
        if key == "battery":
            return ""
        return json.dumps(GOOD[key])

    checks = {c.key: c for c in PC.run(flaky)}
    assert checks["defender"].status == PC.UNKNOWN and "Access is denied" in checks["defender"].summary
    assert checks["firewall"].status == PC.UNKNOWN and checks["battery"].status == PC.UNKNOWN and checks["disks"].status == PC.OK
    text = PC.describe(list(checks.values()))
    assert "3 unknown" in text and "not that it is fine" in text and text.index("[unknown]") < text.index("[ok]")
    assert [c.key for c in PC.run(runner(GOOD), only=("battery", "disks"))] == ["disks", "battery"]
    problem = PC.describe(PC.run(runner({**GOOD, "firewall": [{"Name": "Public", "Enabled": False}]})))
    assert problem.startswith("Your PC needs attention") and problem.splitlines()[1].startswith("  [PROBLEM] Firewall")


def test_the_powershell_runner_refuses_anything_but_the_built_in_scripts(monkeypatch):
    import sys

    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(RuntimeError) as e:
        PC.run_powershell("Remove-Item C:\\ -Recurse")
    assert "built-in" in str(e.value)
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(RuntimeError):
        PC.run_powershell(PC.SCRIPTS["firewall"])
    src = Path("app/core/pc_checkup.py").read_text(encoding="utf-8")
    assert "shell=False" in src and "shell=True" not in src


# ------------------------------------------------------------------ registration


def test_new_tools_are_registered_read_only_and_the_daily_panel_uses_them():
    from app.security.permissions import RiskLevel
    from app.tools.catalog import build_default_registry
    from app.ui.command_center import panel_specs as ps

    reg = build_default_registry()
    for name in ("needs_me", "commitments", "environment_report", "pc_checkup"):
        assert reg.get(name) is not None and reg.get(name).level == RiskLevel.SAFE
    panel = ps.BY_KEY["daily"]
    for item in [*panel.blocks, *panel.actions]:
        reg.get(item.tool).args_model(**item.args)
    assert '("daily"' in Path("app/ui/command_center/widgets.py").read_text(encoding="utf-8")
    from app.config.settings import SECRET_FIELDS, Settings

    assert "imap_password" in SECRET_FIELDS and Settings().imap_user == "" and "imap_password" not in repr(Settings(imap_password="x"))


def test_briefing_mentions_due_promises_only_when_there_are_some(tmp_path):
    from app.tools import assistant as A

    db = mkdb(tmp_path)
    ctx = SimpleNamespace(database=db)
    assert A._promises_line(ctx, NOW) == ""
    C.Ledger(db).add(
        [
            C.Found("i_owe", "Rahim", "I'll send the report.", "2026-11-08", "mail", "r1", "rahim", 1.0),
            C.Found("i_owe", "Karim", "I'll call.", "2026-11-10", "mail", "r2", "karim", 2.0),
            C.Found("i_owe", "Sara", "I'll review.", "2026-12-01", "mail", "r3", "sara", 3.0),
            C.Found("they_owe", "Bank", "Could you send it?", "2026-11-01", "mail", "r4", "bank", 4.0),
        ]
    )
    line = A._promises_line(ctx, NOW)
    assert line.startswith("2 promises you made are due") and "Rahim" in line and "1 more" in line
    assert A._promises_line(SimpleNamespace(database=None), NOW) == ""
