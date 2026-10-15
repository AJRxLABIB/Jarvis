"""Calls and text messages through your phone (Windows Phone Link), and your contacts.

"call Ammu" -> the number from your contacts opens in Phone Link's dialer (tel: link), after you
confirm. "text Rafi I'm on my way" opens a message to him with the text filled in; you press Send.
Contacts live in data\\contacts.json on this PC ("save Ammu's number as 01712345678").

Phone Link has no interface for reading your messages or notifications, so Jarvis can't read
them; it says so instead of pretending.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import quote

from pydantic import Field, field_validator

from app.security.permissions import RiskLevel
from app.tools import winapi
from app.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolError, ToolResult
from app.utils.phonetic import best_sounding

_NUMBER = re.compile(r"^\+?[0-9][0-9 ()-]{5,20}$")
CONTACTS_FILE = "contacts.json"


def _path(ctx: ToolContext) -> Path:
    base = ctx.paths.data_dir if ctx.paths is not None else Path.home() / ".jarvis"
    return base / CONTACTS_FILE


def load_contacts(ctx: ToolContext) -> dict[str, str]:
    try:
        from app.security.protect import read_bytes

        data = json.loads(read_bytes(_path(ctx)).decode("utf-8"))
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def find_contact(ctx: ToolContext, name: str) -> tuple[str, str]:
    contacts = load_contacts(ctx)
    if not contacts:
        raise ToolError("You have no contacts saved yet. Say for example: save Ammu's number as 01712345678.",
                        "no_contacts")
    by_lower = {k.lower(): k for k in contacts}
    key = by_lower.get(name.strip().lower()) or best_sounding(name, list(contacts), threshold=0.75)
    if key is None:
        raise ToolError(f"I don't have a number for {name}. Your contacts: {', '.join(sorted(contacts))}.", "unknown")
    return key, contacts[key]


def _clean(number: str) -> str:
    return re.sub(r"[ ()-]", "", number)


class SaveContactArgs(ToolArgs):
    name: str = Field(min_length=1, max_length=40, description="Contact name, e.g. 'Ammu'")
    number: str = Field(min_length=6, max_length=24, description="Phone number, e.g. +8801712345678")

    @field_validator("number")
    @classmethod
    def _check(cls, value: str) -> str:
        value = value.strip()
        if not _NUMBER.match(value):
            raise ValueError("must be a phone number")
        return value


def save_contact(args: SaveContactArgs, ctx: ToolContext) -> ToolResult:
    contacts = load_contacts(ctx)
    contacts[args.name.strip()] = _clean(args.number)
    path = _path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    from app.security.protect import write_bytes

    write_bytes(path, json.dumps(contacts, ensure_ascii=False, indent=1).encode("utf-8"))  # encrypted (DPAPI)
    return ToolResult.success(f"Saved {args.name}'s number.", name=args.name)


def list_contacts(_: NoArgs, ctx: ToolContext) -> ToolResult:
    contacts = load_contacts(ctx)
    if not contacts:
        return ToolResult.success("You have no contacts saved yet.", contacts=[])
    return ToolResult.success(f"{len(contacts)} contact(s): {', '.join(sorted(contacts))}.", contacts=sorted(contacts))


class CallArgs(ToolArgs):
    name: str = Field(min_length=1, max_length=40, description="Who to call (a saved contact name)")


def _call_question(args: CallArgs, ctx: ToolContext) -> str:
    try:
        name, number = find_contact(ctx, args.name)
        return f"Call {name} ({number}) with your phone?"
    except ToolError:
        return f"Call {args.name} with your phone?"


def call_contact(args: CallArgs, ctx: ToolContext) -> ToolResult:
    winapi.require_windows()
    name, number = find_contact(ctx, args.name)
    winapi.start_file(f"tel:{number}")
    return ToolResult.success(f"Calling {name} through Phone Link.", name=name)


class TextArgs(ToolArgs):
    name: str = Field(min_length=1, max_length=40, description="Who to text (a saved contact name)")
    message: str = Field(min_length=1, max_length=600, description="The message")


def text_contact(args: TextArgs, ctx: ToolContext) -> ToolResult:
    winapi.require_windows()
    name, number = find_contact(ctx, args.name)
    winapi.start_file(f"sms:{number}?body={quote(args.message)}")
    return ToolResult.success(f"Opened a message to {name} with your text; press Send on your phone app to send it.",
                              name=name)


def read_messages(_: NoArgs, ctx: ToolContext) -> ToolResult:
    raise ToolError("I can't read your phone's messages or notifications: Phone Link doesn't let other apps read "
                    "them. You can open Phone Link and I can read the screen for you ('read my screen').",
                    "unsupported")


TOOLS: tuple[Tool, ...] = (
    Tool("save_contact", "Save a contact's phone number (on this PC).", save_contact, SaveContactArgs,
         RiskLevel.MODERATE, category="phone", timeout=10, direct_without_confirmation=lambda a, _: True,
         confirmation=lambda a, _: f"Save {a.name}'s number as {a.number}?"),
    Tool("list_contacts", "The names of the saved contacts.", list_contacts, NoArgs, RiskLevel.SAFE, category="phone"),
    Tool("call_contact", "Call a saved contact through the user's phone (Phone Link).", call_contact, CallArgs,
         RiskLevel.MODERATE, category="phone", timeout=15, confirmation=_call_question),
    Tool("text_contact", "Start a text message to a saved contact (the user presses Send).", text_contact, TextArgs,
         RiskLevel.MODERATE, category="phone", timeout=15,
         confirmation=lambda a, _: f"Write to {a.name}: \"{a.message[:200]}\"?"),
    Tool("read_messages", "Read the phone's messages or notifications (not possible: explains why).", read_messages,
         NoArgs, RiskLevel.SAFE, category="phone"),
)
