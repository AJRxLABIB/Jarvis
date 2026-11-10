"""Read mail from ANY mailbox over IMAP (Gmail, Yahoo, Outlook.com, a company or university server) - not only the Outlook desktop app.

READ-ONLY by construction: the folder is opened with ``readonly=True`` and messages are fetched with ``BODY.PEEK``, so nothing is marked read, moved, flagged or
deleted, and this module has no code path that sends or changes mail. Only the newest ``count`` messages are fetched, each capped (headers + first 20 KB of
body); attachments are never downloaded - only their file names are noted.

Setup (in .env): IMAP_HOST, IMAP_USER, IMAP_PASSWORD (+ IMAP_PORT 993, IMAP_FOLDER INBOX). Gmail / Yahoo / iCloud need an "app password" made in the account's
security page (the normal password is refused when 2-step verification is on); the password is stored DPAPI-encrypted like the other secrets. TLS only (IMAP4_SSL
with certificate checking) - a plain-text connection is never tried.

Mail text comes from other people: it is data for the model, never instructions (callers add that note and the triage flags injection attempts).

Honest status: the parsing is tested on built messages and the IMAP conversation with a fake server object; it has NOT been run against a real mailbox here.
"""

from __future__ import annotations

import email
import email.policy
import html
import re
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any, Callable

MAX_FETCH = 30
BODY_BYTES = 20_000
PREVIEW_CHARS = 600
TIMEOUT_S = 20.0
PROVIDERS = {"gmail.com": "imap.gmail.com", "googlemail.com": "imap.gmail.com", "yahoo.com": "imap.mail.yahoo.com", "outlook.com": "outlook.office365.com",
             "hotmail.com": "outlook.office365.com", "live.com": "outlook.office365.com", "icloud.com": "imap.mail.me.com", "me.com": "imap.mail.me.com",
             "zoho.com": "imap.zoho.com", "aol.com": "imap.aol.com"}
_TAGS = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.S | re.I)
_SPACE = re.compile(r"[ \t\r\f\v]+")
_QUOTED = re.compile(r"\n(?:On .{5,120} wrote:|-{2,}\s*Original Message\s*-{2,}|From: .{3,120}\nSent: ).*", re.S)
_LIST_ROW = re.compile(rb'\((?P<flags>[^)]*)\)\s+(?:"(?P<sep>[^"]*)"|NIL)\s+(?P<name>.+)$')


class MailError(Exception):
    """Safe to show."""


@dataclass(frozen=True)
class Account:
    host: str
    user: str
    password: str
    port: int = 993
    folder: str = "INBOX"

    @classmethod
    def from_settings(cls, settings: Any) -> "Account | None":
        user, password = str(getattr(settings, "imap_user", "") or "").strip(), str(getattr(settings, "imap_password", "") or "")
        host = str(getattr(settings, "imap_host", "") or "").strip() or guess_host(user)
        if not (host and user and password):
            return None
        return cls(host, user, password, int(getattr(settings, "imap_port", 993) or 993), str(getattr(settings, "imap_folder", "") or "INBOX"))


def guess_host(address: str) -> str:
    """The IMAP server of the big providers from the mail address; "" when unknown (then IMAP_HOST must be set)."""
    return PROVIDERS.get(address.rpartition("@")[2].lower(), "") if "@" in address else ""


def _header(value: Any) -> str:
    try:
        return _SPACE.sub(" ", str(make_header(decode_header(str(value or ""))))).strip()
    except Exception:
        return _SPACE.sub(" ", str(value or "")).strip()


def html_to_text(markup: str) -> str:
    text = re.sub(r"<(br|/p|/div|/tr|/li|/h\d)\b[^>]*>", "\n", markup, flags=re.I)
    return html.unescape(_TAGS.sub(" ", text))


def clean_body(text: str, limit: int = PREVIEW_CHARS) -> str:
    """The part the sender wrote now: quoted earlier mail is cut, whitespace squeezed, capped."""
    text = _QUOTED.sub("", "\n" + text.replace("\r\n", "\n"))
    lines = [_SPACE.sub(" ", line).strip() for line in text.split("\n") if not line.lstrip().startswith(">")]
    return re.sub(r"\n{2,}", "\n", "\n".join(line for line in lines if line)).strip()[:limit]


def parse_message(raw: bytes, uid: str = "") -> dict[str, Any]:
    """One RFC 5322 message -> the fields the tools use. Never raises on odd mail: missing parts become empty strings."""
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    plain, rich, attachments = "", "", []
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.is_multipart():
            continue
        name = part.get_filename()
        if name or "attachment" in str(part.get("Content-Disposition", "")).lower():
            attachments.append(_header(name or "attachment")[:80])
            continue
        kind = part.get_content_type()
        if kind not in ("text/plain", "text/html") or (plain if kind == "text/plain" else rich):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload[:BODY_BYTES * 4].decode(part.get_content_charset() or "utf-8", "replace")
        except (LookupError, ValueError):
            text = (part.get_payload(decode=True) or b"")[:BODY_BYTES * 4].decode("utf-8", "replace")
        if kind == "text/plain":
            plain = text
        else:
            rich = text
    body = clean_body(plain or html_to_text(rich))
    senders = getaddresses([str(msg.get("From", ""))])
    name, address = (senders[0] if senders else ("", ""))
    try:
        when = parsedate_to_datetime(str(msg.get("Date", "")))
        received = when.astimezone().strftime("%Y-%m-%d %H:%M") if when.tzinfo else when.strftime("%Y-%m-%d %H:%M")
        stamp = when.timestamp()
    except (TypeError, ValueError, OverflowError):
        received, stamp = "", 0.0
    return {"uid": uid, "from": (_header(name) or address)[:80], "address": address.lower()[:120],
            "to": ", ".join(a.lower() for _, a in getaddresses([str(msg.get("To", ""))]) if a)[:300],
            "subject": _header(msg.get("Subject", ""))[:150] or "(no subject)", "received": received, "timestamp": stamp, "preview": body[:300], "body": body,
            "attachments": attachments[:10], "message_id": str(msg.get("Message-ID", "")).strip()[:200], "in_reply_to": str(msg.get("In-Reply-To", "")).strip()[:200]}


def parse_list_row(row: bytes) -> tuple[set[str], str]:
    """One IMAP LIST answer -> (flags, folder name)."""
    m = _LIST_ROW.match(row.strip())
    if not m:
        return set(), ""
    name = m.group("name").strip().decode("utf-8", "replace").strip('"')
    return {f.lower() for f in m.group("flags").decode("ascii", "replace").split()}, name


def sent_folder(rows: list[bytes]) -> str:
    """The Sent folder: the one flagged \\Sent (RFC 6154), else a well-known name."""
    names = []
    for row in rows:
        flags, name = parse_list_row(row if isinstance(row, bytes) else bytes(str(row), "utf-8"))
        if "\\sent" in flags:
            return name
        names.append(name)
    for wanted in ("[Gmail]/Sent Mail", "Sent", "Sent Items", "Sent Messages", "INBOX.Sent"):
        if wanted in names:
            return wanted
    return ""


def _connect(account: Account):
    import imaplib
    import ssl

    return imaplib.IMAP4_SSL(account.host, account.port, ssl_context=ssl.create_default_context(), timeout=TIMEOUT_S)


def fetch(account: Account, count: int = 10, unread_only: bool = True, folder: str = "", connect: Callable[[Account], Any] = _connect) -> list[dict[str, Any]]:
    """The newest messages, newest first. ``folder="sent"`` reads the Sent folder (found by its flag). Read-only."""
    count = max(1, min(int(count), MAX_FETCH))
    try:
        box = connect(account)
    except Exception as exc:
        raise MailError(f"Could not reach the mail server {account.host} ({exc.__class__.__name__}). Check IMAP_HOST / the internet connection.") from exc
    try:
        try:
            status, _ = box.login(account.user, account.password)
        except Exception as exc:
            raise MailError("The mail server refused the sign-in. Gmail, Yahoo and iCloud need an app password (made in the account's security settings), "
                            "and IMAP must be switched on for the account.") from exc
        if status != "OK":
            raise MailError("The mail server refused the sign-in.")
        target = account.folder
        if folder == "sent":
            status, rows = box.list()
            target = sent_folder(list(rows or [])) if status == "OK" else ""
            if not target:
                raise MailError("The Sent folder could not be found on this mail server.")
        status, _ = box.select('"' + target.replace('"', "") + '"', readonly=True)          # readonly: nothing is ever marked or changed
        if status != "OK":
            raise MailError(f"The folder '{target}' could not be opened.")
        status, data = box.uid("search", None, "UNSEEN" if unread_only and folder != "sent" else "ALL")
        if status != "OK":
            raise MailError("The mail server could not list the messages.")
        uids = (data[0] or b"").split()[-count:][::-1]
        out = []
        for uid in uids:
            status, parts = box.uid("fetch", uid, f"(BODY.PEEK[HEADER] BODY.PEEK[TEXT]<0.{BODY_BYTES}>)")
            if status != "OK" or not parts:
                continue
            raw = b"".join(p[1] for p in parts if isinstance(p, tuple) and len(p) > 1 and isinstance(p[1], (bytes, bytearray)))
            if raw:
                out.append(parse_message(raw, uid.decode("ascii", "replace")))
        return out
    finally:
        try:
            box.logout()
        except Exception:
            pass
