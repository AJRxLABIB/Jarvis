"""Regenerate docs/USER_GUIDE.md from the real tool registry: python scripts/make_user_guide.py"""

import re
from pathlib import Path

from app.tools.catalog import build_default_registry

TITLES = {"basic": "Everyday basics", "apps": "Apps and windows", "web": "Web", "files": "Files and folders", "windows": "Windows settings and devices", "system": "PC health and security",
          "memory": "Memory, knowledge and learning", "automation": "Routines, missions and automation", "phone": "Your phone", "voice": "Voice", "office": "Office, mail and calendar",
          "dev": "Development", "info": "Weather, news and media", "todo": "To-do and focus", "vision": "Screen and photos", "screen": "Clicking and typing", "browser": "Browser pages",
          "personal": "Wellness and journal", "clipboard": "Clipboard", "home": "Smart home", "finance": "Money", "notes": "Notes", "evolution": "Self-improvement", "approvals": "Approvals"}
UNVERIFIED = [
    "Reading and sending mail on a real mailbox (set IMAP_USER / IMAP_PASSWORD or the SMTP settings first).",
    "Interface-change detection (`ui_drift`) on real programs, and the Windows file-audit events (need admin and recording on).",
    "Screen history and click teaching on Windows; the object detector on real photos.",
    "Phone features on a real phone, including the Android build.",
    "The 3D core on your graphics driver (if it crashes, update the driver or set VISUAL_QUALITY=balanced).",
    "How well the small local models handle your own questions: run `python run.py --longmem-eval --full` and `--ground-trial <vision model>`.",
]


def first_sentence(text: str) -> str:
    one = re.split(r"(?<=[.!?])\s", " ".join((text or "").split()), maxsplit=1)[0]
    return one[:200]


def main() -> None:
    reg = build_default_registry()
    groups: dict[str, list] = {}
    for t in reg.tools:
        groups.setdefault(t.category, []).append(t)
    out = ["# Jarvis user guide", "", "Generated from the real tool list; ask for these in plain words. Each line is a thing Jarvis can do.",
           "Risky actions always ask first. Say \"stop\" or use the emergency stop at any time.", ""]
    for cat in sorted(groups, key=lambda c: TITLES.get(c, c)):
        out += [f"## {TITLES.get(cat, cat.title())}", ""]
        out += [f"- **{t.name.replace('_', ' ')}**: {first_sentence(t.description)}" for t in sorted(groups[cat], key=lambda t: t.name)]
        out.append("")
    out += ["## What is not verified yet", "", "These are built and tested with fakes, but have not been checked on real devices:", ""] + [f"- {u}" for u in UNVERIFIED] + [""]
    Path(__file__).resolve().parents[1].joinpath("docs", "USER_GUIDE.md").write_text("\n".join(out), encoding="utf-8")
    print(f"{sum(len(v) for v in groups.values())} tools in {len(groups)} groups")


if __name__ == "__main__":
    main()
