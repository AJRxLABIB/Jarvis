"""Windows Settings pages, classic system tools, and Windows Update - by keyword.

"bluetooth", "open display settings", "wifi settings", "device manager", "scan windows update".
Every page is a fixed ms-settings: address or a fixed system tool from the lists below; nothing
the AI writes is ever run. Opening a page changes nothing (SAFE). Windows Update: your own
command checks, downloads and installs updates without asking (Windows Update does the work
and says if a restart is needed; Jarvis never restarts by itself); if the AI decides on its own,
it asks first.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field

from app.security.permissions import RiskLevel
from app.tools import winapi
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult
from app.utils.phonetic import best_sounding

# display name -> (address, keywords)
PAGES: dict[str, tuple[str, tuple[str, ...]]] = {
    "Windows Update": ("ms-settings:windowsupdate", ("windows update", "update", "updates", "windows upgrade",
                                                     "update settings", "উইন্ডোজ আপডেট")),
    "Bluetooth & devices": ("ms-settings:bluetooth", ("bluetooth", "bluetooth settings", "devices", "ব্লুটুথ")),
    "Wi-Fi": ("ms-settings:network-wifi", ("wifi", "wi-fi", "wi fi", "wireless", "wifi settings", "ওয়াইফাই")),
    "Network & internet": ("ms-settings:network", ("network", "internet settings", "network settings", "ethernet")),
    "VPN": ("ms-settings:network-vpn", ("vpn",)),
    "Mobile hotspot": ("ms-settings:network-mobilehotspot", ("hotspot", "mobile hotspot")),
    "Airplane mode": ("ms-settings:network-airplanemode", ("airplane mode", "flight mode")),
    "Display": ("ms-settings:display", ("display", "screen settings", "display settings", "resolution", "brightness",
                                         "scale")),
    "Night light": ("ms-settings:nightlight", ("night light", "night mode", "blue light")),
    "Sound": ("ms-settings:sound", ("sound", "sound settings", "audio settings", "speakers settings")),
    "Notifications": ("ms-settings:notifications", ("notifications", "notification settings")),
    "Focus": ("ms-settings:quiethours", ("focus", "focus assist", "do not disturb")),
    "Power & battery": ("ms-settings:powersleep", ("power", "power settings", "battery settings", "sleep settings",
                                                   "power and sleep", "battery saver")),
    "Storage": ("ms-settings:storagesense", ("storage", "storage settings", "disk space", "storage sense")),
    "Multitasking": ("ms-settings:multitasking", ("multitasking", "snap windows")),
    "Clipboard settings": ("ms-settings:clipboard", ("clipboard settings", "clipboard history")),
    "About this PC": ("ms-settings:about", ("about", "about this pc", "pc info", "system info", "device specifications",
                                             "pc name")),
    "Activation": ("ms-settings:activation", ("activation", "windows activation")),
    "Recovery": ("ms-settings:recovery", ("recovery", "reset this pc")),
    "Apps": ("ms-settings:appsfeatures", ("apps", "installed apps", "apps and features", "uninstall", "programs")),
    "Default apps": ("ms-settings:defaultapps", ("default apps", "default browser", "default programs")),
    "Startup apps": ("ms-settings:startupapps", ("startup apps", "startup", "startup programs")),
    "Personalization": ("ms-settings:personalization", ("personalization", "personalize", "customize")),
    "Background": ("ms-settings:personalization-background", ("background", "wallpaper", "desktop background")),
    "Colors": ("ms-settings:colors", ("colors", "dark mode", "light mode", "theme colors", "accent color")),
    "Themes": ("ms-settings:themes", ("themes", "theme")),
    "Lock screen": ("ms-settings:lockscreen", ("lock screen", "lock screen settings")),
    "Taskbar": ("ms-settings:taskbar", ("taskbar", "taskbar settings")),
    "Fonts": ("ms-settings:fonts", ("fonts",)),
    "Accounts": ("ms-settings:yourinfo", ("accounts", "my account", "account settings")),
    "Sign-in options": ("ms-settings:signinoptions", ("sign-in options", "sign in options", "password settings",
                                                       "pin settings", "windows hello")),
    "Date & time": ("ms-settings:dateandtime", ("date and time", "date & time", "time settings", "clock settings",
                                                 "time zone")),
    "Language & region": ("ms-settings:regionlanguage", ("language", "language settings", "region", "keyboard language")),
    "Typing": ("ms-settings:typing", ("typing", "keyboard settings")),
    "Mouse": ("ms-settings:mousetouchpad", ("mouse", "mouse settings", "touchpad", "touchpad settings")),
    "Printers & scanners": ("ms-settings:printers", ("printers", "printer", "scanner", "printers and scanners")),
    "Camera settings": ("ms-settings:camera", ("camera settings", "webcam settings")),
    "Gaming": ("ms-settings:gaming-gamebar", ("gaming", "game bar", "game mode")),
    "Accessibility": ("ms-settings:easeofaccess", ("accessibility", "ease of access")),
    "Privacy & security": ("ms-settings:privacy", ("privacy", "privacy settings", "security settings", "permissions")),
    "Microphone privacy": ("ms-settings:privacy-microphone", ("microphone settings", "microphone privacy", "mic settings")),
    "Location": ("ms-settings:privacy-location", ("location", "location settings", "gps")),
    "Windows Security": ("windowsdefender:", ("windows security", "defender", "antivirus", "virus scan",
                                              "windows defender", "firewall")),
    "Settings": ("ms-settings:", ("settings", "windows settings", "system settings", "সেটিংস")),
}

# display name -> (program in System32, keywords)
TOOLS_APPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "Device Manager": ("devmgmt.msc", ("device manager", "drivers")),
    "Disk Management": ("diskmgmt.msc", ("disk management", "partitions")),
    "Services": ("services.msc", ("services", "windows services")),
    "Event Viewer": ("eventvwr.msc", ("event viewer", "event log", "event logs")),
    "Task Scheduler": ("taskschd.msc", ("task scheduler",)),
    "Control Panel": ("control.exe", ("control panel",)),
    "System Information": ("msinfo32.exe", ("system information", "msinfo")),
    "Disk Cleanup": ("cleanmgr.exe", ("disk cleanup", "clean disk", "cleanup")),
    "Resource Monitor": ("resmon.exe", ("resource monitor",)),
    "Performance Monitor": ("perfmon.exe", ("performance monitor",)),
    "Snipping Tool": ("ms-screenclip:", ("snipping tool", "snip", "screen snip")),
}


def _norm(text: str) -> str:
    text = re.sub(r"[^\w&ঀ-৿ -]", " ", (text or "").lower())
    text = re.sub(r"\b(?:open|show|go to|launch|the|my|please|page|section|window|app|menu)\b", " ", text)
    return " ".join(text.split())


def find_page(name: str, *, fuzzy: bool = True) -> tuple[str, str, str] | None:
    """("page", display name, address) or ("tool", display name, program), for a keyword."""
    wanted = _norm(name)
    if not wanted:
        return None
    entries: list[tuple[str, str, str, str]] = []
    for display, (address, words) in PAGES.items():
        for word in (display.lower(), *words):
            entries.append((word, "page", display, address))
    for display, (program, words) in TOOLS_APPS.items():
        for word in (display.lower(), *words):
            entries.append((word, "tool", display, program))
    for word, kind, display, target in entries:
        if wanted == word or wanted == f"{word} settings":
            return kind, display, target
    if not fuzzy:
        return None
    guess = best_sounding(wanted, [e[0] for e in entries], threshold=0.82)
    if guess is None:
        return None
    word, kind, display, target = next(e for e in entries if e[0] == guess)
    return kind, display, target


def _open(kind: str, target: str) -> None:
    if kind == "tool" and not target.endswith(":"):
        winapi.start_file(winapi._system32(target))
    else:
        winapi.start_file(target)


class PageArgs(ToolArgs):
    page: str = Field(min_length=2, max_length=60,
                      description="A Windows Settings page or system tool, e.g. 'bluetooth', 'display', "
                                  "'windows update', 'device manager', 'default apps'")


def open_settings_page(args: PageArgs, ctx: ToolContext) -> ToolResult:
    winapi.require_windows()
    found = find_page(args.page)
    if found is None:
        raise ToolError(f"I don't know a Settings page called '{args.page}'. Try e.g. bluetooth, display, sound, "
                        "wifi, storage, default apps, windows update or device manager.", "unknown_page")
    kind, display, target = found
    _open(kind, target)
    return ToolResult.success(f"Opened {display}.", page=display)


class UpdateArgs(ToolArgs):
    action: Literal["install", "check", "list"] = Field(
        default="install", description="install = check, download and install updates now; check = only open "
                                       "Windows Update and check; list = say which updates are waiting")


def windows_update(args: UpdateArgs, ctx: ToolContext) -> ToolResult:
    winapi.require_windows()
    if args.action == "list":
        try:
            titles = winapi.pending_updates()
        except Exception as exc:
            raise ToolError(f"Windows Update could not be asked right now ({exc.__class__.__name__}).",
                            "update_unavailable") from exc
        if not titles:
            return ToolResult.success("Windows is up to date: no updates are waiting.", updates=[])
        shown = "; ".join(titles[:6]) + (f" and {len(titles) - 6} more" if len(titles) > 6 else "")
        return ToolResult.success(f"{len(titles)} update(s) waiting: {shown}. Say yes to install them.",
                                  updates=titles, suggestion={"tool": "windows_update",
                                                              "arguments": {"action": "install"}})
    winapi.start_file("ms-settings:windowsupdate-action")  # opens Windows Update and starts checking
    if args.action == "check":
        return ToolResult.success("Opened Windows Update and started checking for updates.")
    # Microsoft Defender's "Security Intelligence" updates install fastest through Defender's own updater
    defender, detail = winapi.update_defender_signatures()
    note = (f" Microsoft Defender's security intelligence: {detail}." if defender
            else f" (Defender's own update didn't run: {detail}.)")
    try:
        winapi.start_windows_update()
    except ToolError:
        return ToolResult.success("Opened Windows Update and started checking. Windows will download and install "
                                  "what it finds (press 'Download & install' there if it waits)." + note,
                                  started=False, defender_updated=defender)
    return ToolResult.success("Windows Update is checking, downloading and installing updates now. It will tell you "
                              "if a restart is needed; I won't restart by myself." + note, started=True,
                              defender_updated=defender)


TOOLS: tuple[Tool, ...] = (
    Tool("open_settings_page", "Open a Windows Settings page or a system tool by name: bluetooth, wifi, display, "
         "sound, notifications, power, storage, apps, default apps, startup apps, background, dark mode, taskbar, "
         "date and time, language, mouse, printers, privacy, windows security, device manager, services, "
         "control panel, disk cleanup, event viewer ...", open_settings_page, PageArgs, RiskLevel.SAFE,
         category="windows", timeout=15),
    Tool("windows_update", "Windows Update: install (check, download and install updates now), check (only look), or "
         "list the updates that are waiting. This is the ONLY way to install updates (also Defender/KB updates): "
         "never run_command for them.", windows_update, UpdateArgs, RiskLevel.MODERATE, category="windows",
         timeout=300, confirmation=lambda a, _: "Check for Windows updates and install them now? (Windows may ask for "
         "administrator permission for the Defender update: press Yes there.)" if a.action == "install"
         else "Open Windows Update?",
         direct_without_confirmation=lambda a, _: True),  # your own command: no question (as you asked)
)
