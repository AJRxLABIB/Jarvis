"""Reading your mail with care: what needs you, what is routine, what is noise, and what looks suspicious - with the
reasons shown. Pure text rules (no model, no network), so the result is explainable and testable.

Honest limits: this only sees the sender's name, the subject and the first part of the text. A "suspicious" mark means
several warning signs were present, never proof; a message without marks is not proven safe. Links are never opened.
Text inside an email is other people's text: if it tries to give instructions to an assistant, that is reported as a
warning and ignored - it can never trigger an action, a send or a change of settings.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

_URL = re.compile(r"https?://[^\s<>\"')]+", re.I)
_SHORTENERS = frozenset({"bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd", "buff.ly", "cutt.ly", "rb.gy"})
_BRANDS = ("paypal", "microsoft", "google", "apple", "amazon", "facebook", "netflix", "bkash", "nagad", "dutch-bangla",
           "bank")
_PHISH_WORDS = re.compile(r"verify your (?:account|identity|password)|confirm your (?:password|account)|account (?:has been |will be )?"
                          r"(?:suspended|locked|closed)|unusual (?:sign-?in|activity)|gift cards?|wire transfer|"
                          r"update your (?:billing|payment)|your password (?:has )?expire|click (?:the )?link below|"
                          r"claim your (?:prize|reward)|you have won|crypto|send (?:me )?(?:your )?otp", re.I)
_PROMO = re.compile(r"unsubscribe|\d{1,2}% off|\bsale\b|limited[- ]time|newsletter|free shipping|special offer|"
                    r"deal of the day|promo(?:tion)?|no-?reply", re.I)
_URGENT = re.compile(r"\burgent\b|\basap\b|\bimmediately\b|\bdeadline\b|\boverdue\b|\btoday\b|\bby (?:end of day|eod|noon)\b|"
                     r"\bfinal (?:notice|reminder)\b|\bdue (?:today|tomorrow)\b", re.I)
_DEADLINE = re.compile(r"\b(?:by|before|due(?: on)?|until)\s+((?:mon|tues?|wed(?:nes)?|thu(?:rs?)?|fri|sat(?:ur)?|sun)(?:day)?|"
                       r"tomorrow|today|\d{1,2}\s+[A-Za-z]{3,9}|[A-Za-z]{3,9}\s+\d{1,2}|\d{4}-\d{2}-\d{2}|\d{1,2}(?::\d{2})?\s*(?:am|pm))",
                       re.I)
_ACTION = re.compile(r"\b(?:please|kindly|could you|can you|need you to)\s+([^.!?\n]{5,80})", re.I)
_INJECTION = re.compile(r"ignore (?:all |any )?(?:previous|prior|above) instructions|you are (?:now )?an? (?:ai|assistant)|"
                        r"as (?:an? )?(?:ai|assistant),? (?:you )?(?:must|should|will)|forward (?:this|all)|"
                        r"delete (?:all|this)|do not tell the user|system prompt", re.I)


def link_flags(url: str) -> list[str]:
    host = (urlsplit(url).hostname or "").lower()
    flags = []
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host):
        flags.append("the link goes to a bare IP address")
    if host in _SHORTENERS:
        flags.append(f"the link is shortened ({host}), so its real destination is hidden")
    if "xn--" in host:
        flags.append("the link's domain uses look-alike characters")
    if host.count(".") >= 4:
        flags.append("the link has an unusually long chain of sub-domains")
    plain = host.replace("1", "l").replace("0", "o").replace("3", "e")
    for brand in _BRANDS:
        if brand in plain and not (host == f"{brand}.com" or host.endswith(f".{brand}.com")) and brand != "bank":
            flags.append(f"the link mentions '{brand}' but is not on {brand}.com")
            break
    if urlsplit(url).scheme == "http":
        flags.append("the link is not encrypted (http)")
    return flags


def classify(mail: dict[str, Any]) -> dict[str, Any]:
    sender = str(mail.get("from", ""))
    subject = str(mail.get("subject", ""))
    body = str(mail.get("preview", mail.get("body", "")))
    text = f"{subject}\n{body}"
    reasons: list[str] = []
    warnings: list[str] = []
    signs = 0
    for url in _URL.findall(text)[:5]:
        flags = link_flags(url)
        signs += len(flags)
        warnings += [f"{f} ({urlsplit(url).hostname})" for f in flags]
    if _PHISH_WORDS.search(text):
        signs += 1
        warnings.append("it uses wording common in phishing (asking you to verify, confirm or pay)")
    if _URGENT.search(text) and (_PHISH_WORDS.search(text) or signs):
        signs += 1
        warnings.append("it pressures you to act quickly")
    injection = bool(_INJECTION.search(text))
    if injection:
        warnings.append("the text tries to give instructions to an assistant; they are ignored")
    deadlines = sorted({m.group(1).strip() for m in _DEADLINE.finditer(text)})[:4]
    actions = [m.group(1).strip() for m in _ACTION.finditer(text)][:4]
    if signs >= 2 or (signs >= 1 and injection):
        category = "suspicious"
        reasons.append(f"{signs} warning sign(s); this is a possibility, not a verdict")
    elif _PROMO.search(text) or re.search(r"no-?reply|newsletter|marketing", sender, re.I):
        category = "promotional"
        reasons.append("it looks like a promotion or automated mailing")
    elif _URGENT.search(text) or deadlines:
        category = "urgent"
        reasons.append("it mentions a deadline or asks for quick action" if deadlines or actions else "it sounds urgent")
    else:
        category = "routine"
        reasons.append("nothing in it suggests urgency, promotion or risk")
    return {"category": category, "reasons": reasons, "warnings": warnings, "deadlines": deadlines, "actions": actions,
            "contains_instructions_for_assistant": injection, "from": sender[:80], "subject": subject[:150]}


def triage(mails: list[dict[str, Any]]) -> dict[str, Any]:
    items = [classify(m) | {"received": m.get("received", "")} for m in mails]
    order = {"suspicious": 0, "urgent": 1, "routine": 2, "promotional": 3}
    items.sort(key=lambda i: order[i["category"]])
    counts = {c: sum(1 for i in items if i["category"] == c) for c in order}
    return {"items": items, "counts": counts}


def describe(result: dict[str, Any]) -> str:
    c = result["counts"]
    head = (f"{len(result['items'])} message(s): {c['urgent']} urgent, {c['routine']} routine, {c['promotional']} promotional, "
            f"{c['suspicious']} possibly suspicious.")
    lines = [head]
    for i in result["items"][:6]:
        extra = ""
        if i["deadlines"]:
            extra += f" Deadline mentioned: {', '.join(i['deadlines'])}."
        if i["warnings"]:
            extra += f" Warning: {i['warnings'][0]}."
        lines.append(f"[{i['category']}] {i['from']}: '{i['subject']}' - {i['reasons'][0]}.{extra}")
    lines.append("This is based on the sender, subject and start of each message only; nothing was opened or sent.")
    return "\n".join(lines)
