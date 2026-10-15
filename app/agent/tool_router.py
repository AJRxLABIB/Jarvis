"""Chooses which tool groups to offer for a request.

Small local models pick tools much more reliably from 6-10 relevant tools than
from 25. The router is a hint, never a permission: every call still goes through
the executor. When nothing matches, the everyday groups are offered (FALLBACK).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable

from app.agent.tool_manager import ToolRegistry
from app.ai.base import ToolSpec

ALWAYS = frozenset({"basic"})
KEYWORDS: dict[str, tuple[str, ...]] = {
    "apps": ("this window", "close this", "new tab", "next tab", "full screen", "copy", "paste", "undo", "zoom",
             "open", "launch", "start", "close", "quit", "exit", "switch", "minimi", "maximi", "restore", "window",
             "app", "application", "program", "chrome", "code", "spotify", "whatsapp", "discord", "notepad", "desktop",
             "layout", "side by side", "snap", "left side", "right side", "on the left", "on the right",
             "second screen", "other screen", "monitor", "arrange"),
    "web": ("search", "google", "youtube", "website", "site", "browser", "web", "url", "gmail", "github", "look up",
            "latest", "news", "who is", "what is", "how to", "internet", "online", "documentation", "docs",
            "research", "find out", "sources", "summari", "video", "compare"),
    "files": ("file", "folder", "pdf", "doc", "document", "download", "desktop", "picture", "photo", "video", "music",
              "rename", "move", "copy", "delete", "remove", "create", "make", "find", "size", "list", "show", "open",
              "organi", "tidy", "clean up", "recycle", "zip", "unzip", "extract", "merge", "compress", "shrink",
              "convert", "jpg", "png", "webp", "photos", "word to pdf", "docx"),
    "system": ("cpu", "ram", "memory", "battery", "process", "disk", "storage", "space", "network", "internet speed",
               "gpu", "uptime", "running", "usage", "using", "kill", "force", "performance", "temperature"),
    "windows": ("volume", "mute", "unmute", "sound", "louder", "quieter", "lock", "sleep", "restart", "reboot",
                "shut", "shutdown", "sign out", "log out", "logout", "power", "settings", "setting", "update",
                "upgrade", "bluetooth", "wifi", "wi-fi", "display", "brightness", "brighter", "dimmer", "dim ",
                "ভলিউম", "সাউন্ড", "আওয়াজ", "ব্রাইটনেস", "উজ্জ্বলতা", "wallpaper", "dark mode", "taskbar",
                "device manager", "driver", "control panel", "defender", "firewall", "printer", "storage",
                "default app", "startup app", "notification", "night light", "hotspot", "vpn", "light mode",
                "radio", "ওয়াইফাই", "ব্লুটুথ"),
    "browser": ("page", "read", "article", "click", "tab", "type", "fill", "browser", "website", "site",
                "summari", "link", "button", "scroll", "back", "tell me", "what's there", "whats there",
                "what is there", "what's on", "explain", "docs", "documentation", ".com", ".org", ".io", ".dev",
                "http", "www"),
    "automation": ("remind", "reminder", "schedule", "every day", "every morning", "every night", "every week",
                   "every monday", "weekday", "daily", "o'clock", "tomorrow", " am", " pm", "at 1", "at 2", "at 3",
                   "at 4", "at 5", "at 6", "at 7", "at 8", "at 9", "minutes", "hour", "battery reaches",
                   "battery drops", "battery below", "workflow", "routine", "automation", "environment", "start my",
                   "goal", "mission", "prepare", "plan ", "deadline", "this month", "this week", "লক্ষ্য"),
    "vision": ("screen", "screenshot", "screen shot", "this error", "error message", "this page", "this window",
               "explain this", "summarise this", "summarize this", "what does this say", "read this", "snip", "ocr", "capture", "looking at", "look at", "can you see",
               "see my", "see this", "see what", "what's this", "whats this", "what is this", "this error",
               "error message", "read this", "read the text", "read what", "what does it say", "what does this say",
               "what's written", "whats written", "window say", "window says"),
    "info": ("weather", "temperature outside", "rain", "forecast", "hot today", "cold today", "umbrella", "sunny",
             "briefing", "brief me", "good morning", "my day", "music", "song", "track", "play", "pause", "resume",
             "skip", "spotify", "video", "news", "headline", "queue", "ad ", "ads", "captions", "subtitles",
             "forward", "rewind", "repeat", "like this"),
    "memory": ("remember", "forget", "recall", "memory", "memories", "know about me", "saved", "remind me what",
               "did i say", "did i tell", "we talked", "last time", "last week", "yesterday", "was i doing",
               "did i do", "i was working", "left off", "before lunch", "this morning", "my document",
               "my documents", "my notes", "my files", "pdf", "thesis", "index", "according to", "in the document",
               "working on", "context", "what am i doing", "what do you understand", "decision", "related to",
               "which files", "which project", "worked on"),
    "screen": ("click", "double click", "right click", "press", "type ", "scroll", "button", "shortcut", "ctrl",
               "alt+", "enter key", "fill", "select the", "tap on", "tick", "untick", "check the box", "ok button"),
    "phone": ("call", "phone", "ring", "dial", "sms", "text message", "message to", "contact", "contacts", "number",
              "whatsapp", "messages", "texted", "messaged"),
    "home": ("light", "lights", "fan", "switch", "lamp", "ac", "air condition", "curtain", "tv", "door", "lock the",
             "smart home", "turn on the", "turn off the", "bulb", "লাইট", "ফ্যান", "বাতি"),
    "voice": ("nickname", "call it", "means", "mistake", "review", "fix", "setting", "turn on", "turn off",
              "dictation", "dictate", "type what i say", "enroll", "wake word", "train", "self test", "speed report",
              "jarvis model", "update jarvis", "hide", "show yourself", "new conversation", "suggestion", "memory",
              "interpret", "translat", "quiz", "study", "test me"),
    "clipboard": ("clipboard", "copied", "i copied", "what i copy", "paste", "copy it", "copy that", "copy this",
                  "copy the", "কপি"),
    "todo": ("to-do", "todo", "to do list", "my list", "on my list", "task list", "tasks", "checklist", "add to",
             "mark ", "done with", "finished", "shopping list", "লিস্ট", "তালিকা", "কাজের", "focus", "concentrate",
             "do not disturb", "quiet time", "deep work", "pomodoro", "মনোযোগ"),
    "notes": ("notes", "note taking", "take notes", "lecture", "class notes", "meeting notes", "record the meeting",
              "transcribe", "minutes of", "নোট", "লেকচার"),
    "office": ("calendar", "meeting", "meetings", "schedule today", "appointment", "email", "emails", "mail",
               "inbox", "outlook", "send a mail", "reply to"),
    "dev": ("git", "commit", "branch", "status", "test", "npm", "pip", "python", "node", "project", "repo", "code",
            "build", "install", "dependenc", "server", "dev", "editor", "vs code", "vscode", "run"),
    "evolution": ("improve yourself", "improve your", "upgrade yourself", "proposal", "self-improv", "self improv",
                  "your own code", "your code", "your source", "roll back", "rollback", "evolution", "fix yourself"),
}


def categories_for(text: str) -> set[str]:
    lowered = f" {text.lower()} "
    found = set()
    for category, words in KEYWORDS.items():
        if any(re.search(rf"\b{re.escape(w)}", lowered) for w in words):
            found.add(category)
    return found


# Nothing in the request names a tool group ("thanks", "how are you"): offer the everyday groups only.
# All 98 tools are ~14,000 tokens - more than a 6,144-token context holds, and slow to read every time.
FALLBACK = frozenset({"apps", "info", "web"})


def spec_tokens(spec: ToolSpec) -> int:
    """Rough token count of one tool description as the model sees it (~3.3 characters per token)."""
    return int(len(json.dumps({"name": spec.name, "description": spec.description, "parameters": spec.parameters},
                              ensure_ascii=False)) / 3.3) + 8


def select_specs(registry: ToolRegistry, text: str, recent_tools: Iterable[str] = (),
                 max_tokens: int | None = None) -> list[ToolSpec]:
    """The tools to offer for ``text``: the groups it names, the groups just used, always the basics;
    ``max_tokens`` caps the total (named groups first, then recent ones)."""
    named = categories_for(text)
    recent: list[str] = []
    for name in recent_tools:  # follow-ups like "now close it" keep the previous group
        tool = registry.get(name)
        if tool is not None and tool.category not in named and tool.category not in recent:
            recent.append(tool.category)
    order = [*sorted(named), *recent] or sorted(FALLBACK)
    if not named and recent:
        order += [c for c in sorted(FALLBACK) if c not in order]
    # Tools from groups the router has no keywords for (plugins, custom tools) are always offered.
    pairs = list(zip(registry.specs(), registry.tools))
    chosen = [spec for spec, tool in pairs if tool.category in ALWAYS or tool.category not in KEYWORDS]
    budget = None if max_tokens is None else max_tokens - sum(spec_tokens(s) for s in chosen)
    for category in order:
        group = [spec for spec, tool in pairs if tool.category == category and tool.category not in ALWAYS]
        cost = sum(spec_tokens(s) for s in group)
        if budget is not None:
            if cost > budget and category not in named:
                continue  # a recent/fallback group that doesn't fit is left out
            if cost > budget:  # a named group always goes in (the request needs it) - trimmed if it must be
                group = _fit(group, budget)
                cost = sum(spec_tokens(s) for s in group)
            budget -= cost
        chosen += group
    names = {spec.name for spec in chosen}
    return [spec for spec in registry.specs() if spec.name in names]


def _fit(group: list[ToolSpec], budget: int) -> list[ToolSpec]:
    kept = []
    for spec in group:
        if spec_tokens(spec) <= budget:
            kept.append(spec)
            budget -= spec_tokens(spec)
    return kept or group[:1]
