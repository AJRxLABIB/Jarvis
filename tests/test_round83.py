import smtplib

import pytest

from app.core import mail_smtp as S


class Settings:
    imap_user, imap_password, smtp_host, smtp_port = "me@gmail.com", "app-pass", "", 0


class FakeClient:
    def __init__(self, refuse=None, auth=True):
        self.refuse, self.auth, self.sent, self.quit_called = refuse or {}, auth, [], False

    def login(self, user, password):
        if not self.auth:
            raise smtplib.SMTPAuthenticationError(535, b"bad")

    def send_message(self, msg):
        self.sent.append(msg)
        return self.refuse

    def quit(self):
        self.quit_called = True


def test_account_guessing_and_ports():
    assert S.account(Settings()) == ("smtp.gmail.com", 465, "me@gmail.com", "app-pass")
    o = type("O", (), {"imap_user": "a@outlook.com", "imap_password": "x", "smtp_host": "", "smtp_port": 0})()
    assert S.account(o)[:2] == ("smtp-mail.outlook.com", 587)
    c = type("C", (), {"imap_user": "a@corp.example", "imap_password": "x", "smtp_host": "", "smtp_port": 0})()
    assert S.account(c) is None                                       # unknown provider and no SMTP_HOST: not guessed
    c.smtp_host, c.smtp_port = "mail.corp.example", 2525
    assert S.account(c)[:2] == ("mail.corp.example", 2525)
    assert S.account(type("N", (), {"imap_user": "", "imap_password": ""})()) is None


def test_send_ok_and_message_shape():
    cl = FakeClient()
    mid = S.send(S.account(Settings()), "you@example.com", "Hello", "Body text", connect=lambda h, p: cl)
    msg = cl.sent[0]
    assert msg["To"] == "you@example.com" and msg["From"] == "me@gmail.com" and msg["Subject"] == "Hello" and mid == msg["Message-ID"]
    assert "Body text" in msg.get_content() and cl.quit_called


def test_send_failures_are_never_reported_as_sent():
    acct = S.account(Settings())
    with pytest.raises(S.SendError, match="refused the sign-in"):
        S.send(acct, "you@example.com", "s", "b", connect=lambda h, p: FakeClient(auth=False))
    with pytest.raises(S.SendError, match="refused the recipient"):
        S.send(acct, "you@example.com", "s", "b", connect=lambda h, p: FakeClient(refuse={"you@example.com": (550, b"no")}))

    def down(h, p):
        raise OSError("no route")

    with pytest.raises(S.SendError, match="Could not reach"):
        S.send(acct, "you@example.com", "s", "b", connect=down)
    with pytest.raises(S.SendError, match="line break"):
        S.send(acct, "you@example.com", "Hi\nBcc: evil@x.com", "b", connect=lambda h, p: FakeClient())


def test_send_email_tool_uses_smtp_when_configured_and_stays_dangerous(monkeypatch):
    from app.security.permissions import RiskLevel
    from app.tools import outlook
    from app.tools.base import ToolError
    from app.tools.catalog import build_default_registry
    from app.utils import network

    t = build_default_registry().get("send_email")
    assert t.level == RiskLevel.DANGEROUS and t.confirmation is not None
    sent = []
    monkeypatch.setattr(S, "send", lambda acct, to, subject, body: sent.append((acct[0], to)) or "<id>")
    monkeypatch.setattr(network, "offline_reason", lambda s: None)
    ctx = type("C", (), {"settings": Settings()})()
    out = outlook.send_email(outlook.SendEmailArgs(to="you@example.com", subject="s", body="b"), ctx)
    assert out.ok and out.data["via"] == "smtp" and sent == [("smtp.gmail.com", "you@example.com")]
    monkeypatch.setattr(network, "offline_reason", lambda s: "You are offline.")
    with pytest.raises(ToolError):
        outlook.send_email(outlook.SendEmailArgs(to="you@example.com", subject="s", body="b"), ctx)
    assert len(sent) == 1

    def fail(*a):
        raise S.SendError("The server refused the recipient: x.")

    monkeypatch.setattr(network, "offline_reason", lambda s: None)
    monkeypatch.setattr(S, "send", fail)
    with pytest.raises(ToolError, match="refused"):
        outlook.send_email(outlook.SendEmailArgs(to="you@example.com", subject="s", body="b"), ctx)


def test_message_items_only_requests_from_message_apps():
    from app.tools import daily_tools as D

    chats = [{"name": "Rima", "unread": 2, "last_message_untrusted": "Can you send me the invoice by Friday?"}, {"name": "Group", "unread": 9, "last_message_untrusted": "lol"}]
    notes = [{"title": "Karim", "body": "Please call me back today", "source": "phone:WhatsApp"}, {"title": "Bank", "body": "Please update your app", "source": "phone:Play Store"},
             {"title": "", "body": "Please reply", "source": "sms"}]
    found = D.message_items(chats, notes, 1_000_000.0)
    who = sorted(f.who for f in found)
    assert who == ["Karim", "Rima"] and all(f.kind == "they_owe" or f.kind for f in found)
    assert {f.source for f in found} == {"whatsapp", "phone"}
    again = D.message_items(chats, notes, 2_000_000.0)
    assert [f.id for f in found] == [f.id for f in again]              # the same message is the same item on the next scan
