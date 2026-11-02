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
            "research", "find out", "sources", "summari", "video", "compare", "research project"),
    "files": ("refinery", "deadlines in", "scan my folder", "extract facts", "file", "folder", "pdf", "doc", "document", "download", "desktop", "picture", "photo", "video", "music",
              "rename", "move", "copy", "delete", "remove", "create", "make", "find", "size", "list", "show", "open",
              "organi", "tidy", "clean up", "recycle", "zip", "unzip", "extract", "merge", "compress", "shrink",
              "convert", "jpg", "png", "webp", "photos", "word to pdf", "docx", "duplicate", "same file", "copies"),
    "finance": ("recurring", "savings", "forecast my", "month end", "import csv", "csv", "expense", "expenses", "bank sms", "bkash", "nagad", "sms payments", "import them", "spent", "spend", "spending", "budget", "money", "cost", "paid", "pay for",
                "taka", " tk", "৳", "খরচ", "বাজেট"),
    "personal": ("wellness", "sleep hours", "energy level", "my mood", "journal", "diary", "note to self", "voice note", "write down", "jot", "habit", "habits", "streak",
                 "drink water", "exercise", "workout", "meditat", "did my", "mood"),
    "system": ("is my pc safe", "suspicious activity", "security check", "what changed on my pc", "what changed while", "while i was away", "since i left", "while i was gone", "interrupt me", "do not disturb", "transaction", "across devices", "phone and pc task", "situation", "guardian", "snapshot", "unfinished", "resume my work", "my devices", "all devices", "connected devices", "on my network", "discover", "performance mode", "battery saver", "fit in memory", "cpu", "ram", "memory", "battery", "process", "disk", "storage", "space", "network", "internet speed",
               "gpu", "uptime", "running", "usage", "using", "kill", "force", "performance", "temperature",
               "health", "normal for my pc", "activity", "timeline", "what did you do", "what did you change",
               "not verified", "profile", "settings profile", "meeting mode", "night mode", "performance mode", "switch to", "troubleshoot", "not working", "doesn't work", "fix my setup", "what's wrong", "tamper", "log intact", "which program", "before the crash", "before it crashed", "trace", "integration", "connected", "voice stats", "how is voice"),
    "windows": ("volume", "mute", "unmute", "sound", "speaker", "speakers", "headphones", "bluetooth", "output device", "audio output", "louder", "quieter", "lock", "sleep", "restart", "reboot",
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
    "automation": ("skill card", "show my routine", "what does this workflow do", "is this skill safe", "simulate", "simulation", "what if", "what would happen", "background mode", "in the background", "run without the window", "remind", "reminder", "schedule", "every day", "every morning", "every night", "every week",
                   "every monday", "weekday", "daily", "o'clock", "tomorrow", " am", " pm", "at 1", "at 2", "at 3",
                   "at 4", "at 5", "at 6", "at 7", "at 8", "at 9", "minutes", "hour", "battery reaches",
                   "battery drops", "battery below", "workflow", "routine", "automation", "environment", "start my",
                   "goal", "mission", "prepare", "plan ", "deadline", "this month", "this week", "লক্ষ্য",
                   "export workflow", "import workflow", "resume "),
    "vision": ("screen", "screenshot", "screen shot", "this error", "error message", "this page", "this window",
               "explain this", "summarise this", "summarize this", "what does this say", "read this", "snip", "ocr", "capture", "looking at", "look at", "can you see",
               "see my", "see this", "see what", "what's this", "whats this", "what is this", "this error",
               "error message", "read this", "read the text", "read what", "what does it say", "what does this say",
               "what's written", "whats written", "window say", "window says"),
    "info": ("weather", "temperature outside", "rain", "forecast", "hot today", "cold today", "umbrella", "sunny",
             "briefing", "brief me", "good morning", "my day", "music", "song", "track", "play", "pause", "resume",
             "skip", "spotify", "video", "news", "headline", "queue", "ad ", "ads", "captions", "subtitles",
             "forward", "rewind", "repeat", "like this"),
    "memory": ("remember", "forget", "voice recording", "my recordings", "voice data", "learned", "learnt", "tune your ai", "smarter", "upgrade your brain", "getting better", "improving", "where did you get", "is that verified", "source", "mention that", "why did you say", "import memor", "edit memor", "change memor", "recall", "memory", "memories", "know about me", "what context", "your evidence", "why did you assume", "saved", "remind me what",
               "did i say", "did i tell", "we talked", "last time", "last week", "yesterday", "was i doing",
               "did i do", "i was working", "left off", "before lunch", "this morning", "my document",
               "my documents", "my notes", "my files", "pdf", "thesis", "index", "according to", "in the document",
               "working on", "context", "what am i doing", "what do you understand", "decision", "related to",
               "which files", "which project", "worked on", "learn", "flashcard", "revision", "spaced",
               "export my memories", "memory categor", "search everything"),
    "screen": ("click", "double click", "right click", "press", "type ", "scroll", "button", "shortcut", "ctrl",
               "alt+", "enter key", "fill", "select the", "tap on", "tick", "untick", "check the box", "ok button"),
    "phone": ("call", "phone", "ring", "dial", "sms", "gallery", "camera on my", "share your screen", "share my screen", "text message", "message to", "contact", "contacts", "number",
              "whatsapp", "messages", "texted", "messaged", "sync", "android", "jarvis mobile", "my phone", "flashlight", "torch",
              "find my phone", "mobile"),
    "home": ("light", "lights", "plug", "socket", "shelly", "tasmota", "wled", "led strip", "brightness", "fan", "switch", "lamp", "ac", "air condition", "curtain", "tv", "door", "lock the",
             "smart home", "turn on the", "turn off the", "bulb", "লাইট", "ফ্যান", "বাতি"),
    "voice": ("nickname", "call it", "means", "mistake", "review", "fix", "setting", "turn on", "turn off",
              "dictation", "dictate", "type what i say", "enroll", "wake word", "train", "self test", "speed report",
              "jarvis model", "update jarvis", "hide", "show yourself", "new conversation", "suggestion", "memory",
              "interpret", "translat", "quiz", "study", "test me"),
    "approvals": ("don't ask me again", "dont ask me again", "stop asking", "ask me again", "standing approval", "pre-approve",
                  "preapprove", "approvals", "always allow", "stop confirming"),
    "clipboard": ("clipboard", "copied", "translate this", "fix the grammar", "summarise what", "summarize what", "i copied", "what i copy", "paste", "copy it", "copy that", "copy this",
                  "copy the", "কপি"),
    "todo": ("to-do", "todo", "to do list", "my list", "on my list", "task list", "tasks", "checklist", "add to",
             "mark ", "done with", "finished", "shopping list", "লিস্ট", "তালিকা", "কাজের", "focus", "concentrate",
             "do not disturb", "quiet time", "deep work", "pomodoro", "মনোযোগ", "plan my day", "replan",
             "review my day", "what should i do", "time estimate", "minutes for"),
    "notes": ("notes", "note taking", "take notes", "lecture", "class notes", "meeting notes", "record the meeting",
              "transcribe", "minutes of", "নোট", "লেকচার"),
    "office": ("excel", "spreadsheet", "workbook", "sheet", "cells", "google", "google drive", "google calendar", "drive file", "my drive", "calendar", "meeting", "meetings", "schedule today", "appointment", "email", "emails", "mail",
               "inbox", "outlook", "send a mail", "reply to", "triage", "draft", "phishing", "suspicious", "urgent email", "conflict", "overlap", "double booked", "double-booked", "clash"),
    "dev": ("start my server", "is my project running", "development environment", "why did it crash", "dev server", "git", "commit", "branch", "status", "test", "npm", "pip", "python", "node", "project", "repo", "code",
            "build", "install", "dependenc", "server", "dev", "editor", "vs code", "vscode", "run", "github", "pull request", "pull requests", "assigned to me"),
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


_TOKEN_CACHE: dict[int, tuple[ToolSpec, int]] = {}


def spec_tokens(spec: ToolSpec) -> int:
    """Rough token count of one tool description as the model sees it (~3.3 characters per token). Cached per spec
    object (the cache keeps the spec alive, so an id can never be reused for a different one)."""
    hit = _TOKEN_CACHE.get(id(spec))
    if hit is not None and hit[0] is spec:
        return hit[1]
    tokens = int(len(json.dumps({"name": spec.name, "description": spec.description, "parameters": spec.parameters},
                                ensure_ascii=False)) / 3.3) + 8
    if len(_TOKEN_CACHE) > 2000:
        _TOKEN_CACHE.clear()
    _TOKEN_CACHE[id(spec)] = (spec, tokens)
    return tokens


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
    all_specs = registry.specs()
    pairs = list(zip(all_specs, registry.tools))
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
    return [spec for spec in all_specs if spec.name in names]


def _fit(group: list[ToolSpec], budget: int) -> list[ToolSpec]:
    kept = []
    for spec in group:
        if spec_tokens(spec) <= budget:
            kept.append(spec)
            budget -= spec_tokens(spec)
    return kept or group[:1]
