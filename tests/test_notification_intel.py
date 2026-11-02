from __future__ import annotations

import pytest

from app.core import notification_intel as N


def c(app, title, text=""):
    return N.Classifier().classify(app, title, text)


@pytest.mark.parametrize(("app", "title", "text", "label"), [
    ("Bank", "Security alert", "New sign-in from Chrome on Windows", N.CRITICAL),
    ("Bank", "Payment failed", "Your card was declined", N.CRITICAL),
    ("Gmail", "Invoice 442", "Payment due by 5 pm", N.ACTION),
    ("Mail", "Please confirm", "Reply with yes to approve", N.ACTION),
    ("Daraz", "Flash sale", "50% off everything, limited time", N.PROMO),
    ("Calendar", "Meeting at 3", "Project review", N.IMPORTANT),
    ("Courier", "Out for delivery", "Arriving today", N.IMPORTANT),
    ("WhatsApp", "Rafi", "bhai kal dekha hobe", N.IMPORTANT),
    ("Weather", "Sunny", "31 degrees", N.ROUTINE),
])
def test_labels(app, title, text, label):
    assert c(app, title, text).label == label


def test_phishing_is_suspicious_and_says_why():
    r = c("SMS", "Account suspended", "Your account has been suspended. Click here http://bit.ly/x9 immediately to verify your account")
    assert r.label == N.SUSPICIOUS and r.priority == "high" and any("link" in x or "verify" in x for x in r.reasons)
    assert "Suspicious" in r.headline()
    assert c("SMS", "বার্তা", "আপনার অ্যাকাউন্ট বন্ধ হয়ে যাবে, লিংকে ক্লিক করুন https://x.co").label == N.SUSPICIOUS


def test_a_real_security_alert_without_phishing_cues_is_critical_not_suspicious():
    assert c("Google", "Security alert", "New sign-in from a Windows device").label == N.CRITICAL


def test_duplicates_ignore_digits_and_are_counted_once_in_the_digest():
    clf = N.Classifier()
    a = clf.classify("Chat", "Rafi", "call me back 1")
    b = clf.classify("Chat", "Rafi", "call me back 2")
    assert not a.duplicate and b.duplicate
    text = N.digest([a, b, clf.classify("Bank", "Payment failed", "declined")])
    assert "2 notification(s)" in text and "1 critical" in text and "1 repeat(s) ignored" in text


def test_deadline_phrase_and_reminder_hint_are_suggestions_only():
    r = c("Mail", "Assignment", "Submit it by 5 pm tomorrow, deadline strict")
    assert "5 pm" in r.deadline or "tomorrow" in r.deadline
    assert "Say \"remind me" in N.reminder_hint(r) and N.reminder_hint(c("Weather", "Sunny")) == ""


def test_digest_orders_by_attention_and_hides_noise():
    clf = N.Classifier()
    rs = [clf.classify("Daraz", "Sale", "50% off"), clf.classify("Weather", "Sunny", "ok"), clf.classify("Bank", "Fraud alert", "unauthorized charge"),
          clf.classify("Mail", "Invoice", "payment due")]
    lines = N.digest(rs).splitlines()
    assert lines[1].startswith("- [CRITICAL]") and lines[2].startswith("- [Action required]") and "plus 2 routine / promotional" in lines[-1]


def test_classify_stored_reads_phone_titles():
    rows = [{"title": "Bank: Payment failed", "body": "declined", "source": "phone:Pixel"}, {"title": "Daraz: Sale", "body": "70% off", "source": "phone:Pixel"}]
    out = N.classify_stored(rows)
    assert [r.label for r in out] == [N.CRITICAL, N.PROMO] and out[0].app == "Bank"


def test_phone_notifications_are_classified_on_arrival(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from app.core.notifications import NotificationCenter
    from app.memory.database import Database
    from app.remote.server import RemoteServer

    db = Database(tmp_path / "n.db")
    db.initialize()
    N.CLASSIFIER._seen.clear()
    server = SimpleNamespace(hub=SimpleNamespace(mobile=SimpleNamespace(database=db)))
    device = SimpleNamespace(id="phone-0001", name="Pixel", trust="trusted", permissions=[])

    def push(app, title, text):
        return asyncio.run(RemoteServer._mobile_run(server, device, "phone_notify", {"app": app, "title": title, "text": text}))

    assert push("Daraz", "Flash sale", "50% off")["filtered"] == "promotional"
    assert push("Bank", "Fraud alert", "unauthorized charge of 500")["label"] == "critical"
    assert push("Bank", "Fraud alert", "unauthorized charge of 900")["filtered"] == "duplicate"
    assert push("Mail", "Invoice", "Payment due by 5 pm")["label"] == "action_required"
    items = NotificationCenter(db).items()
    titles = {i.title: (i.category, i.priority) for i in items}
    assert titles["[CRITICAL] Bank: Fraud alert"] == ("security", "urgent")
    assert titles["[Action required] Mail: Invoice"] == ("phone", "high")
    assert not any("Daraz" in t for t in titles)
