"""Send one plain-text mail over SMTP with the same account the IMAP reader uses (IMAP_USER + the app password). The counterpart of ``mail_imap``.

Only ever reached from the ``send_email`` tool, which is DANGEROUS level: the full message is shown and you confirm every single send. This module
never decides to send anything. TLS only (implicit TLS on 465, or STARTTLS on 587 - a server that offers neither is refused). One recipient, plain text,
no attachments. Header injection is impossible by construction (``EmailMessage`` rejects line breaks in headers; they are also checked here).
Outlook.com / Microsoft 365 accounts often refuse password sign-in for SMTP (they need OAuth): the server's own refusal is passed on as it is.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any, Callable

SMTP_HOSTS = {"gmail.com": "smtp.gmail.com", "googlemail.com": "smtp.gmail.com", "yahoo.com": "smtp.mail.yahoo.com", "outlook.com": "smtp-mail.outlook.com",
              "hotmail.com": "smtp-mail.outlook.com", "live.com": "smtp-mail.outlook.com", "icloud.com": "smtp.mail.me.com", "me.com": "smtp.mail.me.com",
              "zoho.com": "smtp.zoho.com", "aol.com": "smtp.aol.com"}
STARTTLS_ONLY = {"smtp-mail.outlook.com", "smtp.mail.me.com"}
TIMEOUT = 25


class SendError(RuntimeError):
    pass


def guess_host(address: str) -> str:
    return SMTP_HOSTS.get(address.rpartition("@")[2].lower(), "") if "@" in address else ""


def account(settings: Any) -> tuple[str, int, str, str] | None:
    """(host, port, user, password) or None when mail is not set up."""
    user, password = str(getattr(settings, "imap_user", "") or "").strip(), str(getattr(settings, "imap_password", "") or "")
    host = str(getattr(settings, "smtp_host", "") or "").strip() or guess_host(user)
    if not (host and user and password):
        return None
    port = int(getattr(settings, "smtp_port", 0) or 0) or (587 if host in STARTTLS_ONLY else 465)
    return host, port, user, password


def build(sender: str, to: str, subject: str, body: str) -> EmailMessage:
    for name, value in (("recipient", to), ("subject", subject), ("sender", sender)):
        if "\n" in value or "\r" in value:
            raise SendError(f"The {name} contains a line break, so nothing was sent.")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    msg["Date"], msg["Message-ID"] = formatdate(localtime=True), make_msgid(domain=sender.rpartition("@")[2] or None)
    msg.set_content(body)
    return msg


def _connect(host: str, port: int):
    ctx = ssl.create_default_context()
    if port == 465:
        return smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=ctx)
    client = smtplib.SMTP(host, port, timeout=TIMEOUT)
    client.ehlo()
    if not client.has_extn("starttls"):
        client.close()
        raise SendError("The mail server does not offer encryption (STARTTLS), so nothing was sent.")
    client.starttls(context=ctx)
    client.ehlo()
    return client


def send(acct: tuple[str, int, str, str], to: str, subject: str, body: str, connect: Callable[[str, int], Any] = _connect) -> str:
    """Send one message; returns its Message-ID. Raises SendError with the server's reason - a refusal is never reported as sent."""
    host, port, user, password = acct
    msg = build(user, to, subject, body)
    try:
        client = connect(host, port)
    except SendError:
        raise
    except (OSError, smtplib.SMTPException) as exc:
        raise SendError(f"Could not reach the mail server {host}:{port} ({type(exc).__name__}).") from exc
    try:
        try:
            client.login(user, password)
        except smtplib.SMTPAuthenticationError as exc:
            raise SendError("The mail server refused the sign-in. Gmail, Yahoo and iCloud need an app password; Outlook.com accounts usually do not allow "
                            "password sign-in for sending.") from exc
        refused = client.send_message(msg)
        if refused:
            raise SendError(f"The server refused the recipient: {', '.join(refused)}.")
    except SendError:
        raise
    except (OSError, smtplib.SMTPException) as exc:
        raise SendError(f"The mail was not sent ({type(exc).__name__}: {str(exc)[:120]}).") from exc
    finally:
        try:
            client.quit()
        except Exception:
            pass
    return str(msg["Message-ID"])
