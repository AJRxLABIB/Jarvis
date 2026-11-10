# Round 77 - daily life: mail from any provider, promises, "what needs me", surroundings, PC check-up

## What the research said, and what was already here

The 2026 direction for personal assistants is proactive and ambient: instead of answering questions they watch mail, messages, calendar and device signals and
surface what matters, with sending gated on approval; local-first deployment is a growing preference. Open-source inbox agents track follow-ups, mostly the
promises YOU made; tracking what others owe you is rarer. Windows itself is adding opt-in agent features. (Sources are mostly vendor and review pages, so they were
used for direction, not for facts.)

JARVIS already had most of that list: mail triage, WhatsApp unread chats, phone notification classification, calendar, briefing, weather, news, PC health history,
the guardian, to-dos, reminders - 287 tools. The audit found five real gaps, and those were built.

## Built

1. **Mail from any mailbox (`core/mail_imap.py`).** Mail worked only through the Outlook desktop app. Now `read_emails` / `triage_emails` read Gmail, Yahoo,
   Outlook.com, iCloud or any IMAP server when `IMAP_USER` and `IMAP_PASSWORD` are set in `.env` (`IMAP_HOST` is worked out for the big providers). Read-only by
   construction: the folder is opened read-only, messages are fetched with `BODY.PEEK` (nothing is marked read), TLS only, at most 30 messages, body capped,
   attachments only named. The password is an **app password** from the mail account's security page and is encrypted like the other secrets.
2. **Promises ledger (`core/commitments.py`, migration 43, tool `commitments`).** Finds "I'll send it by Friday" in mail you sent, requests made to you, and
   things you asked others for; gives a due date only when the sentence names one; shows overdue first; a later message from the person who owed it marks the item
   "probably settled" for you to confirm. Each item quotes its sentence (one sentence is stored, not the mail). English plus a few Bangla forms. Rule-based: it
   will miss unusual wording and sometimes keep a polite phrase - that is why items can be dismissed and never come back.
3. **What needs me (`core/needs_me.py`, tool `needs_me`).** One ranked list across unread mail, WhatsApp, phone notifications, promises and to-dos, with the reason
   for each place and a fixed, documented order (suspicious items first so you do not act on them). Sources that could not be read are named as NOT checked.
4. **Surroundings (`core/environment.py`, tool `environment_report`).** Air quality (US AQI, PM2.5, PM10), UV, feels-like heat, rain in the next 6 hours and
   daylight for your city, with the standard advice for each band (EPA AQI bands, WHO UV bands). From Open-Meteo, like the weather tool (free, no account; only
   coordinates are sent). The morning briefing adds one sentence when the AQI is above 100, and one when promises are due.
5. **PC check-up (`core/pc_checkup.py`, tool `pc_checkup`).** Antivirus, firewall, Windows updates, pending restart, disk health and wear, battery wear, start-up
   programs, free space: each ok / attention / problem / unknown with evidence and the threshold used. Read-only: fixed PowerShell status scripts, no user text in
   a command, nothing changed. A reading Windows does not give (often needs administrator rights) is "unknown", never "ok".

A **Daily** panel in the command center shows all four.

## Set-up for mail

In `.env`: `IMAP_USER=you@gmail.com`, `IMAP_PASSWORD=<app password>`. For Gmail: Google Account > Security > 2-Step Verification > App passwords, and IMAP must be
enabled in Gmail settings. Then "read my email", "triage my email", "scan my mail for promises", "what needs me".

## Not built, and why

* Sending or deleting mail over IMAP/SMTP: reading was the gap; `send_email` (Outlook, approval-gated) is unchanged.
* Reading WhatsApp / SMS content into the promises ledger: the WhatsApp reader only sees the chat list preview, and SMS reading goes through the phone on request;
  the ledger accepts those sources, but only mail is scanned automatically today.
* Recall-style continuous screen history: privacy cost is high and `screen_watch` / activity history already cover the opt-in part.

## Verification

`tests/test_round77.py` - 24 tests with fakes (built messages, a fake IMAP server object, recorded-shape PowerShell JSON, a fake weather service).
**Not verified on real systems:** a real mailbox (Gmail app password, folder names), the PowerShell scripts on your Windows, the live Open-Meteo air-quality
answer for your city. Expect to send me the first error if one of those differs from what was assumed.
