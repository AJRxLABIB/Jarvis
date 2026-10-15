"""Direct commands: very common, unambiguous requests are handled without the AI model.

"open WhatsApp" / "close Chrome" -> the matching tool call, when the name is an
app the assistant actually knows. The call still goes through the tool executor
(validation, permissions, audit); only the model's choice of tool is skipped,
which makes these commands faster and immune to small-model tool mix-ups.
Everything else goes to the model as usual.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable

from app.ai.base import ToolCall

_FILLER = r"(?:please\s+|can you\s+|could you\s+|would you\s+|now\s+|just\s+)*"
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(rf"^{_FILLER}(?:open|launch|start|run)\s+(?:the\s+|my\s+)?(?P<name>.+?)(?:\s+app(?:lication)?)?\s*(?:please)?[.!?]*$", re.I), "open_application"),
    (re.compile(rf"^{_FILLER}(?:close|quit|exit)\s+(?:the\s+|my\s+)?(?P<name>.+?)(?:\s+app(?:lication)?)?\s*(?:please)?[.!?]*$", re.I), "close_application"),
)
_NOT_APPS = {"it", "that", "this", "them", "everything", "all", "file", "folder", "the file", "the folder"}


# "lock my PC" (and how speech recognition often writes it: "block my PC", "log my laptop")
_LOCK = re.compile(rf"^{_FILLER}(?:lock|block|log|look|love|luck|lok|lack)\s*(?:up\s+)?(?:my\s+|the\s+|this\s+)?"
                   r"(?:pc|p\.?\s?c\.?|computer|laptop|screen|system|device|machine|windows?|workstation)"
                   r"(?:\s+now)?\s*(?:please)?[.!?]*$|^(?:lock)[.!]*$", re.I)
_IN_BROWSER = re.compile(rf"^{_FILLER}(?:open|show|go to|launch)\s+(?:the\s+|my\s+)?(?P<name>.+?)\s+(?:in|on|with|using)"
                         r"\s+(?:the\s+|my\s+)?(?P<browser>google chrome|chrome|microsoft edge|edge|firefox|"
                         r"mozilla firefox|browser|default browser)\s*(?:please)?[.!?]*$", re.I)
_OPEN_SITE = re.compile(rf"^{_FILLER}(?:open|show|go to|launch)\s+(?:the\s+|my\s+)?(?P<name>.+?)"
                        r"(?:\s+(?:website|site|app))?\s*(?:please)?[.!?]*$", re.I)

# "restart my PC", "shut down the computer", "save my work and restart"
_DEVICE = r"(?:pc|p\.?\s?c\.?|computer|laptop|system|device|machine|windows)"
_POWER = re.compile(
    rf"^{_FILLER}(?P<save>save(?:\s+(?:my|all|the|all\s+my))?(?:\s+(?:work|files|everything|documents|stuff))?"
    r"\s*,?\s*(?:and|then)\s+)?(?P<verb>restart|reboot|shut\s*down|power\s+off|turn\s+off)"
    rf"(?:\s+(?:my|the|this))?(?P<device>\s+{_DEVICE})?(?:\s+now)?\s*(?:please)?[.!?]*$", re.I)
_CANCEL_POWER = re.compile(r"^(?:please\s+)?(?:cancel|abort|stop|don'?t|do\s+not|wait)(?:\s+(?:it|that|the))?"
                           r"(?:\s+(?:restart|reboot|shutdown|shut\s*down|restarting|shutting\s+down))?[.!]*$", re.I)
_EXPLICIT_CANCEL = re.compile(r"^(?:cancel|abort|stop)\s+(?:the\s+)?(?:restart|reboot|shutdown|shut\s*down)[.!]*$",
                              re.I)

# "minimize Chrome", "switch to WhatsApp", "maximize VS Code"
_WINDOW = re.compile(rf"^{_FILLER}(?P<verb>minimi[sz]e|maximi[sz]e|restore|switch\s+to|focus(?:\s+on)?|bring\s+up)"
                     r"\s+(?:the\s+|my\s+)?(?P<name>.+?)(?:\s+app(?:lication)?|\s+window)?\s*(?:please)?[.!?]*$", re.I)
_MINIMIZE_ALL = re.compile(rf"^{_FILLER}(?:minimi[sz]e\s+(?:all|everything)(?:\s+(?:the\s+)?windows)?|show\s+(?:me\s+)?"
                           r"(?:the\s+)?desktop)\s*(?:please)?[.!?]*$", re.I)
# instant answers, no model needed
_TIME = re.compile(r"^(?:(?:hey\s+)?(?:what(?:'s|\s+is)\s+the\s+time|what\s+time\s+is\s+it|tell\s+me\s+the\s+time|"
                   r"(?:what(?:'s|\s+is)\s+)?(?:the\s+)?current\s+time|what(?:'s|\s+is)\s+(?:the\s+|today'?s\s+)?date"
                   r"(?:\s+today)?|what\s+day\s+is\s+(?:it|today)|what(?:'s|\s+is)\s+today))(?:\s+(?:now|please))?[.!?]*$", re.I)
_BATTERY = re.compile(r"^(?:(?:what(?:'s|\s+is)|how\s+much(?:\s+is)?|check|show)\s+(?:my\s+|the\s+)?battery"
                      r"(?:\s+(?:level|percentage|status|left|life))?|battery(?:\s+(?:level|percentage|status))?|"
                      r"how\s+much\s+battery\s+(?:do\s+i\s+have|is\s+left)(?:\s+left)?)[.!?]*$", re.I)

# "what's the weather", "weather in Dhaka tomorrow", "is it going to rain today"
_WEATHER = re.compile(
    r"^(?:(?:what(?:'s|\s+is)|how(?:'s|\s+is)|tell\s+me|check|show(?:\s+me)?)\s+)?(?:the\s+)?(?:weather|forecast|temperature)"
    r"(?:\s+(?:like|forecast|outside))?(?:\s+(?:in|for|at)\s+(?P<place>[a-z][a-z .,'-]{1,40}?))?"
    r"(?:\s+(?P<when>now|today|tomorrow|right\s+now))?(?:\s+(?:in|for|at)\s+(?P<place2>[a-z][a-z .,'-]{1,40}?))?[.!?]*$"
    r"|^(?:is\s+it|will\s+it)\s+(?:going\s+to\s+)?rain(?:ing)?(?:\s+(?P<when2>today|tomorrow))?[.!?]*$", re.I)
_MEDIA = re.compile(
    r"^(?:please\s+)?(?:(?P<pause>pause|resume|unpause|play)(?:\s+(?:the\s+|my\s+)?(?:music|song|video|track|playback|it|spotify))?"
    r"|(?P<next>next|skip)(?:\s+(?:this\s+|the\s+)?(?:song|track|video))?|(?:play\s+(?:the\s+)?)?(?P<prev>previous|last)\s+"
    r"(?:song|track|video)|(?P<stop>stop)\s+(?:the\s+)?(?:music|song|playback|video))(?:\s+please)?[.!?]*$", re.I)
_VOLUME = re.compile(
    r"^(?:please\s+)?(?:(?:turn\s+(?:the\s+)?(?:volume|sound|it)\s+(?P<updown>up|down))|(?:volume|sound)\s+(?P<updown2>up|down)"
    r"|(?P<loud>louder|quieter|softer)|(?P<mute>mute|unmute)(?:\s+(?:the\s+)?(?:sound|volume|audio|computer|pc|laptop))?"
    r"|(?:set\s+(?:the\s+)?)?(?:volume|sound)\s+(?:to\s+|at\s+)?(?P<level>\d{1,3})(?:\s*(?:%|percent))?)(?:\s+please)?[.!?]*$", re.I)
_BRIEFING = re.compile(
    r"^(?:(?:hey\s+)?good\s+morning|brief\s+me|(?:give\s+me\s+)?(?:my\s+|the\s+)?(?:daily|morning)\s+briefing|"
    r"what(?:'s|\s+is)\s+my\s+day\s+(?:like|look\s+like)|what(?:'s|\s+is)\s+(?:up\s+)?today)"
    r"(?:\s*,?\s*(?:jarvis|sir))?[.!?]*$", re.I)


_ORGANIZE = re.compile(r"^(?:please\s+)?(?:organi[sz]e|tidy(?:\s+up)?|clean\s+up|sort)\s+(?:all\s+)?(?:the\s+|my\s+)?"
                       r"(?:files\s+on\s+(?:the\s+|my\s+)?desktop|desktop(?:\s+files)?)(?:\s+files)?(?:\s+please)?[.!?]*$", re.I)
_UNDO_ORGANIZE = re.compile(r"^(?:please\s+)?(?:undo\s+(?:the\s+)?(?:organi[sz]e|organi[sz]ing|desktop\s+organi[sz]ing)|"
                            r"put\s+(?:my\s+)?(?:desktop\s+)?files\s+back|restore\s+(?:my\s+)?desktop)[.!?]*$", re.I)
_DOWNLOADS = re.compile(r"^(?:please\s+)?(?:organi[sz]e|tidy(?:\s+up)?|clean\s+up|sort)\s+(?:out\s+)?(?:the\s+|my\s+)?"
                        r"downloads?(?:\s+folder)?(?:\s+please)?[.!?]*$", re.I)
_UNDO_DOWNLOADS = re.compile(r"^(?:please\s+)?undo\s+(?:the\s+)?(?:sort(?:ing)?|organi[sz]e|organi[sz]ing)\s+(?:of\s+)?"
                             r"(?:the\s+|my\s+)?downloads?[.!?]*$", re.I)
_RECYCLE = re.compile(r"^(?:please\s+)?(?:empty|clear|clean(?:\s+up)?)\s+(?:the\s+|my\s+)?recycle\s*bin(?:\s+please)?[.!?]*$",
                      re.I)
_NEWS = re.compile(r"^(?:(?:what(?:'s|\s+is)|tell\s+me|give\s+me|read\s+me|show\s+me)\s+)?(?:the\s+|today'?s\s+)?(?:latest\s+)?"
                   r"(?:news|headlines)(?:\s+today)?(?:\s+(?:about|on)\s+(?P<topic>[a-z0-9 .'-]{2,40}))?[.!?]*$", re.I)


# "play a music for me", "play some songs", "play Arijit Singh on YouTube", "play lofi music"
_PLAY_MUSIC = re.compile(
    r"^(?:please\s+|can\s+you\s+|could\s+you\s+|would\s+you\s+)*(?:play|put\s+on|start)\s+"
    r"(?:(?:me\s+)?(?:a|an|some|any|the)?\s*(?:music|song|songs|track|tracks|playlist)(?:\s+for\s+me)?"
    r"(?:\s+(?:from|on)\s+youtube)?"
    r"|(?P<query>(?!music\b|song\b|it\b|that\b|this\b|the\s+next\b|next\b|previous\b)[\w .&'-]{2,60}?)"
    r"(?:\s+(?:song|songs|music))?(?:\s+(?:from|on)\s+youtube)?(?P<yt>)"
    r")(?:\s+(?:for\s+me|please))?[.!?]*$", re.I)


def _play_music(cleaned: str) -> ToolCall | None:
    match = _PLAY_MUSIC.match(cleaned)
    if not match:
        return None
    query = (match.group("query") or "").strip()
    lowered = cleaned.lower()
    if query:
        # "play chess" / "play the game": only names clearly about music or YouTube
        if not re.search(r"\b(song|songs|music|youtube|playlist|track)\b", lowered):
            return None
        return ToolCall("play_music", {"query": query})
    return ToolCall("play_music", {})


def _info_shortcut(cleaned: str) -> ToolCall | None:
    music = _play_music(cleaned)
    if music is not None:
        return music
    if _ORGANIZE.match(cleaned):
        return ToolCall("organize_desktop", {})
    if _UNDO_ORGANIZE.match(cleaned):
        return ToolCall("organize_desktop", {"undo": True})
    if _DOWNLOADS.match(cleaned):
        return ToolCall("organize_downloads", {})
    if _UNDO_DOWNLOADS.match(cleaned):
        return ToolCall("organize_downloads", {"undo": True})
    if _RECYCLE.match(cleaned):
        return ToolCall("empty_recycle_bin", {})
    match = _NEWS.match(cleaned)
    if match:
        return ToolCall("get_news", {"topic": match.group("topic").strip()} if match.group("topic") else {})
    match = _WEATHER.match(cleaned)
    if match:
        when = (match.group("when") or match.group("when2") or "now").lower()
        when = "now" if when == "right now" else when
        if cleaned.lower().startswith(("is it", "will it")) and when == "now":
            when = "today"
        place = (match.group("place") or match.group("place2") or "").strip(" ,.")
        arguments: dict = {"when": when}
        if place and place.lower() not in ("here", "outside", "my area", "my city"):
            arguments["location"] = place
        return ToolCall("get_weather", arguments)
    match = _MEDIA.match(cleaned)
    if match:
        action = ("next" if match.group("next") else "previous" if match.group("prev")
                  else "stop" if match.group("stop") else "play_pause")
        return ToolCall("media_control", {"action": action})
    match = _VOLUME.match(cleaned)
    if match:
        if match.group("level"):
            return ToolCall("volume_control", {"action": "set", "level": min(100, int(match.group("level")))})
        if match.group("mute"):
            return ToolCall("volume_control", {"action": match.group("mute").lower()})
        up = (match.group("updown") or match.group("updown2") or "").lower() == "up" or (
            (match.group("loud") or "").lower() == "louder")
        return ToolCall("volume_control", {"action": "up" if up else "down"})
    if _BRIEFING.match(cleaned):
        return ToolCall("daily_briefing", {})
    return None

_WORKFLOW = re.compile(rf"^{_FILLER}(?:start|run|begin|launch|open)\s+(?:the\s+|my\s+)?(?P<name>.+?)(?:\s+workflow)?\s*(?:please)?[.!?]*$", re.I)


_HOME = re.compile(r"^(?:please\s+)?(?:turn|switch|put)\s+(?P<state>on|off)\s+(?:the\s+|my\s+)?(?P<device>[\w' -]{2,40}?)"
                   r"(?:\s+please)?[.!]*$", re.I)
_HOME_AFTER = re.compile(r"^(?:please\s+)?(?:turn|switch|put)\s+(?:the\s+|my\s+)?(?P<device>[\w' -]{2,40}?)\s+"
                         r"(?P<state>on|off)(?:\s+please)?[.!]*$", re.I)


_NOTES_START = re.compile(r"^(?:please\s+)?(?:(?:start|begin)\s+(?:taking\s+)?(?:the\s+|my\s+)?(?:meeting|lecture|class)?\s*notes|"
                          r"take\s+(?:meeting\s+|lecture\s+|class\s+)?notes|(?:start|begin)\s+(?:the\s+)?(?:meeting|lecture|class)\s+"
                          r"(?:notes|recording)|(?:লেকচার|মিটিং|ক্লাস)?\s*নোট\s*(?:নাও|নেওয়া\s*শুরু\s*করো|শুরু\s*করো|লিখো))"
                          r"(?:\s+please)?[.!।]*$", re.I)
_TODO_ADD = re.compile(r"^(?:please\s+)?(?:add|put)\s+(?P<text>.{2,120}?)\s+(?:to|on)\s+(?:my\s+|the\s+)?"
                       r"(?:to-?do\s+list|to\s+do\s+list|list|todos?)(?:\s+(?:for\s+)?(?P<due>today|tomorrow|"
                       r"(?:on\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)))?[.!]*$", re.I)
_TODO_LIST = re.compile(r"^(?:what(?:'s| is)\s+on\s+my\s+(?:to-?do\s+)?list|(?:show|read)\s+(?:me\s+)?my\s+"
                        r"(?:to-?do\s+)?list|my\s+to-?do\s+list|what\s+do\s+i\s+have\s+to\s+do)[?.!]*$", re.I)


# "scan windows update", "update windows", "check for updates", "upgrade my pc"
_UPDATE = re.compile(
    rf"^{_FILLER}(?:(?:scan|check|run|start|do|install|get|download)\s+(?:for\s+|the\s+|my\s+|all\s+|new\s+)*)?"
    r"(?:windows\s+(?:update|updates|upgrade|upgrades)|updates?|system\s+updates?)(?:\s+now)?\s*(?:please)?[.!?]*$|"
    rf"^{_FILLER}(?:update|upgrade)\s+(?:my\s+|the\s+|this\s+)?(?:windows|pc|computer|laptop|system)"
    r"(?:\s+now)?\s*(?:please)?[.!?]*$|^উইন্ডোজ\s*আপডেট(?:\s*(?:করো|দাও|চেক করো|স্ক্যান করো))?[।.!]*$", re.I)
_UPDATE_LIST = re.compile(r"^(?:which|what|any|are there(?:\s+any)?)\s+(?:windows\s+)?updates?\s*"
                          r"(?:are\s+)?(?:waiting|pending|available|there)?\s*\??$", re.I)
_SETTINGS_PAGE = re.compile(rf"^{_FILLER}(?:open|show|go\s+to|launch|take\s+me\s+to)?\s*(?:the\s+|my\s+)?"
                            r"(?P<name>.+?)\s+(?:settings?|section|page)(?:\s+please)?[.!?]*$", re.I)
_BARE_FILLER = re.compile(r"^(?:please\s+|jarvis[\s,]+|hey\s+)+|(?:[\s,]+please|[\s,]+jarvis)+$|[.!?।]+$", re.I)
# one word is enough for these
BARE_ACTIONS: dict[str, tuple[str, dict]] = {
    "screenshot": ("take_screenshot", {}), "screen shot": ("take_screenshot", {}),
    "mute": ("volume_control", {"action": "mute"}), "unmute": ("volume_control", {"action": "unmute"}),
    "volume": ("volume_control", {"action": "get"}),
    "music": ("play_music", {}), "song": ("play_music", {}), "songs": ("play_music", {}),
    "news": ("get_news", {}), "headlines": ("get_news", {}), "weather": ("get_weather", {}),
    "time": ("get_datetime", {}), "date": ("get_datetime", {}), "battery": ("system_status", {"aspect": "battery"}),
    "system status": ("system_status", {}), "briefing": ("daily_briefing", {}),
    "good morning": ("daily_briefing", {}), "desktop": ("window_control", {"action": "minimize_all"}),
    "show desktop": ("window_control", {"action": "minimize_all"}),
    "pause": ("media_control", {"action": "play_pause"}), "play": ("media_control", {"action": "play_pause"}),
    "resume": ("media_control", {"action": "play_pause"}), "next": ("media_control", {"action": "next"}),
    "next song": ("media_control", {"action": "next"}), "previous": ("media_control", {"action": "previous"}),
    "previous song": ("media_control", {"action": "previous"}), "my list": ("list_todos", {}),
    "to do list": ("list_todos", {}), "todo list": ("list_todos", {}),
}


def bare_keyword(text: str) -> str:
    """ "YouTube." / "Jarvis, Google Chrome please" -> "youtube" / "google chrome" ("" if it is a sentence)."""
    cleaned = _BARE_FILLER.sub("", " ".join(text.split())).strip(" ,")
    return cleaned.lower() if 0 < len(cleaned.split()) <= 4 else ""


def _sounds_like(heard: str, name: str) -> bool:
    from app.utils.phonetic import similarity

    heard, name = heard.lower(), name.lower()
    if len(heard.split()) > len(name.split()):
        return False  # extra words ("google chrome club" = "খোলো" misheard): not just the name
    return heard == name or heard in name or similarity(heard, name) >= 0.8


def match_keyword(text: str, known_app: Callable[[str], str | None],
                  known_site: Callable[[str], bool] | None = None,
                  known_workflow: Callable[[str], str | None] | None = None) -> ToolCall | None:
    """Just a name is a request to open it: "youtube", "google chrome", "bluetooth", "device manager", or to run
    one of your workflows ("good night")."""
    word = bare_keyword(text)
    if not word or word in _NOT_APPS:
        return None
    if known_workflow is not None:
        workflow = known_workflow(word)
        if workflow is not None:
            return ToolCall("run_workflow", {"name": workflow})
    if word in BARE_ACTIONS:
        tool, arguments = BARE_ACTIONS[word]
        return ToolCall(tool, dict(arguments))
    from app.tools.windows_settings import find_page

    page = find_page(word, fuzzy=False)
    if page is not None and page[1] != "Settings":
        return ToolCall("open_settings_page", {"page": word})
    if known_site is not None and known_site(word):
        return ToolCall("open_website", {"site": word})
    app = known_app(word)
    if app is not None and _sounds_like(word, app):
        return ToolCall("open_application", {"name": word})
    if page is not None:
        return ToolCall("open_settings_page", {"page": word})
    return None


# ------------------------------------------------------------------ voice-only control

_NICKNAME = re.compile(r"^(?:please\s+)?(?:call|name|nickname)\s+(?P<target>.+?)\s+(?:as\s+)?[\"'“‘](?P<nick>[^\"'”’]+)"
                       r"[\"'”’]\s*[.!]*$|^(?:please\s+)?(?:call|nickname)\s+(?P<target2>.+?)\s+as\s+(?P<nick2>.+?)[.!]*$|"
                       r"^[\"'“‘]?(?P<nick3>.{1,30}?)[\"'”’]?\s+means\s+(?P<target3>.+?)[.!]*$", re.I)
_THIS_WINDOW = re.compile(r"^(?:please\s+)?(?P<verb>close|minimi[sz]e|maximi[sz]e|restore)\s+(?:this|this\s+window|"
                          r"the\s+current\s+window|this\s+app)(?:\s+please)?[.!]*$|^(?P<which>what(?:'s| is)\s+this\s+"
                          r"(?:window|app)|which\s+(?:window|app)\s+(?:is\s+this|am\s+i\s+in))\??$", re.I)
_MOUSE = re.compile(r"^(?:please\s+)?(?:(?P<click>left\s+click|click|double\s+click|right\s+click)(?:\s+here|\s+it)?|"
                    r"(?:move\s+(?:the\s+)?mouse|mouse)\s+(?P<dir>up|down|left|right)(?:\s+(?P<px>\d{1,4}))?|"
                    r"(?:center|centre)\s+(?:the\s+)?mouse)[.!]*$", re.I)
_SCROLL = re.compile(r"^(?:please\s+)?scroll\s+(?P<dir>up|down)(?:\s+(?P<more>more|a lot))?[.!]*$", re.I)
_DICTATION = re.compile(r"^(?:please\s+)?(?:start\s+(?:dictation|dictating|typing)|type\s+what\s+i\s+say|"
                        r"dictation\s+(?:mode|on)|লিখে\s*যাও)[.!]*$", re.I)
_JARVIS = [
    (re.compile(r"^(?:open|show)\s+(?:your|jarvis'?s?|the\s+jarvis)\s+settings[.!]*$", re.I), "open_settings"),
    (re.compile(r"^(?:open|show)\s+(?:my\s+|the\s+)?memor(?:y|ies)(?:\s+window)?[.!]*$", re.I), "open_memory"),
    (re.compile(r"^(?:open|show)\s+(?:the\s+)?privacy(?:\s+dashboard)?[.!]*$", re.I), "open_privacy"),
    (re.compile(r"^(?:open|show)\s+(?:the\s+|your\s+)?logs?[.!]*$", re.I), "open_logs"),
    (re.compile(r"^(?:open|show)\s+(?:my\s+|the\s+)?automations?[.!]*$", re.I), "open_automations"),
    (re.compile(r"^(?:open|show)\s+(?:me\s+)?(?:the\s+)?(?:mission\s+control|agent\s+trace|what\s+you\s+did)[.!]*$",
                re.I), "mission_control"),
    (re.compile(r"^(?:open|show)\s+(?:the\s+)?system\s+(?:page|tab|stats|details)[.!]*$", re.I), "show_system"),
    (re.compile(r"^(?:open|show)\s+(?:the\s+)?(?:dashboard|core\s+live|main\s+page)[.!]*$", re.I), "show_dashboard"),
    (re.compile(r"^enroll\s+my\s+voice|learn\s+my\s+voice[.!]*$", re.I), "enroll_voice"),
    (re.compile(r"^(?:teach\s+(?:you\s+)?|learn\s+)my\s+wake\s+word[.!]*$", re.I), "teach_wake_word"),
    (re.compile(r"^train\s+(?:my|your)\s+voice(?:\s+recognition)?[.!]*$", re.I), "train_voice"),
    (re.compile(r"^(?:make|build|update|upgrade)\s+(?:the\s+)?jarvis\s+model[.!]*$", re.I), "make_jarvis_model"),
    (re.compile(r"^(?:run\s+(?:a\s+|the\s+)?)?self[\s-]?test[.!]*$", re.I), "self_test"),
    (re.compile(r"^(?:show\s+(?:the\s+)?)?speed\s+report[.!]*$", re.I), "speed_report"),
    (re.compile(r"^check\s+for\s+jarvis\s+updates?|update\s+jarvis[.!]*$", re.I), "check_updates"),
    (re.compile(r"^(?:start|open|show)\s+(?:the\s+)?phone\s+remote[.!]*$", re.I), "phone_remote"),
    (re.compile(r"^(?:new\s+conversation|start\s+over|forget\s+this\s+conversation)[.!]*$", re.I), "new_conversation"),
    (re.compile(r"^(?:please\s+)?(?:hide\s+(?:yourself|your\s+window|the\s+(?:jarvis\s+)?window|jarvis)|"
                r"minimi[sz]e\s+(?:yourself|your\s+window|the\s+jarvis\s+window|jarvis)|"
                r"go\s+to\s+the\s+background|get\s+out\s+of\s+(?:the|my)\s+way|show\s+me\s+my\s+(?:screen|desktop))"
                r"(?:\s+please)?(?:\s*[,.;]?\s*(?:i|so|because)\s+.{0,60})?[.!]*$", re.I), "hide_window"),
    (re.compile(r"^(?:show\s+yourself|show\s+your\s+window|come\s+back|where\s+are\s+you)[.!?]*$", re.I), "show_window"),
    (re.compile(r"^(?:open|run|start)\s+(?:the\s+)?setup\s+wizard[.!]*$", re.I), "setup_wizard"),
    (re.compile(r"^(?:make|run|give\s+me|show)?\s*(?:a\s+|the\s+|your\s+)?health\s+(?:report|check)[.!]*$", re.I),
     "health_report"),
    (re.compile(r"^(?:compare|test)\s+(?:the\s+|my\s+)?(?:ai\s+)?models[.!]*$", re.I), "compare_models"),
    (re.compile(r"^(?:stay|keep\s+yourself|be|always\s+stay)\s+(?:always\s+)?on\s+top(?:\s+of\s+(?:everything|all|other\s+"
                r"windows|chrome))?(?:\s+please)?[.!]*$|^সবার\s+উপরে\s+থাকো[।.!]*$", re.I), "stay_on_top"),
    (re.compile(r"^(?:stop|don'?t\s+(?:stay|keep))\s+(?:staying\s+)?on\s+top(?:\s+please)?[.!]*$|"
                r"^(?:stop\s+being|no\s+longer)\s+on\s+top[.!]*$", re.I), "not_on_top"),
    # Jarvis's own window ("full screen" alone is the app in front: F11, or "f" on YouTube)
    (re.compile(r"^(?:make|put|show|set)\s+(?:yourself|your\s+window|jarvis|the\s+jarvis\s+window)\s+(?:in\s+|to\s+)?"
                r"full[\s-]?screen(?:\s+please)?[.!]*$|^(?:go\s+)?full[\s-]?screen\s+yourself[.!]*$|"
                r"^(?:তুমি\s+)?নিজেকে\s+ফুল\s*স্ক্রিন\s+করো[।.!]*$", re.I), "full_screen"),
    (re.compile(r"^(?:exit|leave|close|stop)\s+(?:your\s+)?full[\s-]?screen\s+(?:yourself|mode\s+yourself)[.!]*$|"
                r"^(?:exit|leave)\s+your\s+full[\s-]?screen(?:\s+mode)?[.!]*$|^(?:make|put)\s+(?:yourself|your\s+window)\s+"
                r"(?:smaller|normal|windowed)[.!]*$|^নিজেকে\s+ছোট\s+করো[।.!]*$", re.I), "exit_full_screen"),
    (re.compile(r"^(?:use|switch\s+to|change\s+to)\s+(?:the\s+)?(?:best|winning|fastest\s+good)\s+(?:ai\s+)?model"
                r"(?:\s+for\s+me)?[.!]*$|^use\s+the\s+winner[.!]*$", re.I), "use_best_model"),
    (re.compile(r"^close\s+(?:the\s+|your\s+)?(?:dialog|pop\s?up|settings|memory|privacy(?:\s+dashboard)?|logs|"
                r"automations|wizard)(?:\s+window)?[.!]*$", re.I), "close_dialog"),
]
_SET = re.compile(r"^(?:please\s+)?(?:(?P<onoff>turn|switch)\s+(?P<state>on|off)\s+(?:the\s+|my\s+)?(?P<name>.+?)|"
                  r"(?:turn|switch)\s+(?:the\s+|my\s+)?(?P<name2>.+?)\s+(?P<state2>on|off)|"
                  r"set\s+(?:the\s+|my\s+)?(?P<name3>.+?)\s+to\s+(?P<value3>.+?))[.!]*$", re.I)
_SUGGESTION = re.compile(r"^(?:(?P<yes>accept|save|keep)\s+(?:the\s+|that\s+)?(?:memory\s+)?suggestion|"
                         r"yes,?\s+remember\s+(?:it|that)|(?P<no>reject|drop|forget)\s+(?:the\s+|that\s+)?(?:memory\s+)?"
                         r"suggestion|don'?t\s+remember\s+that)[.!]*$", re.I)
_REVIEW = re.compile(r"^(?:review\s+your\s+mistakes|what\s+did\s+you\s+get\s+wrong|how\s+did\s+you\s+do\s+this\s+week|"
                     r"weekly\s+review)[.!?]*$", re.I)
_ACCEPT_FIX = re.compile(r"^(?:yes,?\s+)?(?:accept|apply|use)\s+(?:(?:fix|fixes)\s+(?P<num>[\d\s,and]+)|(?P<all>all)\s+"
                         r"(?:the\s+)?fixes|the\s+fix)[.!]*$", re.I)
_WHATSAPP = re.compile(r"^(?:please\s+)?(?:send\s+)?(?:a\s+)?whats\s?app\s+(?:message\s+)?(?:to\s+)?(?P<name>[\wঀ-৿]+"
                       r"(?:\s+[\wঀ-৿]+)?)\s*(?:saying|that|:|,|message)\s*(?P<msg>.{1,500})$", re.I)
_CLEAN_TEMP = re.compile(r"^(?:please\s+)?(?:clean|clear|delete|remove)\s+(?:the\s+|my\s+|all\s+)?(?:old\s+)?temp(?:orary)?"
                         r"\s+files|free\s+up\s+(?:some\s+)?(?:disk\s+)?space[.!]*$", re.I)


def match_voice_control(text: str, known_setting: Callable[[str], bool] | None = None) -> ToolCall | None:
    """Commands for full voice control (no clicking): the window you're in, app shortcuts, the mouse,
    dictation, Jarvis itself, settings, memory suggestions, the weekly review, WhatsApp, tidy-ups."""
    cleaned = " ".join(text.split()).strip()
    whatsapp = _WHATSAPP.match(cleaned)
    if whatsapp:
        return ToolCall("whatsapp_message", {"name": whatsapp.group("name"), "message": whatsapp.group("msg").strip()})
    if len(cleaned) > 80:
        return None
    nick = _NICKNAME.match(cleaned)
    if nick:
        nickname = nick.group("nick") or nick.group("nick2") or nick.group("nick3")
        target = nick.group("target") or nick.group("target2") or nick.group("target3")
        if nickname and target and len(nickname.split()) <= 4:
            quotes = " '\"“”‘’"
            return ToolCall("set_nickname", {"nickname": nickname.strip(quotes), "target": target.strip(quotes)})
    window = _THIS_WINDOW.match(cleaned)
    if window:
        if window.group("which"):
            return ToolCall("this_window", {"action": "which"})
        verb = window.group("verb").lower()
        action = "minimize" if verb.startswith("minimi") else "maximize" if verb.startswith("maximi") else verb
        return ToolCall("this_window", {"action": action})
    mouse = _MOUSE.match(cleaned)
    if mouse:
        if mouse.group("click"):
            kind = mouse.group("click").lower().split()[0]
            return ToolCall("mouse_control", {"action": {"double": "double_click", "right": "right_click"}.get(kind, "click")})
        if mouse.group("dir"):
            arguments = {"action": "move", "direction": mouse.group("dir").lower()}
            if mouse.group("px"):
                arguments["pixels"] = int(mouse.group("px"))
            return ToolCall("mouse_control", arguments)
        return ToolCall("mouse_control", {"action": "center"})
    scroll = _SCROLL.match(cleaned)
    if scroll:
        return ToolCall("scroll_screen", {"direction": scroll.group("dir").lower(), "amount": 12 if scroll.group("more") else 5})
    if _DICTATION.match(cleaned):
        return ToolCall("dictation", {"action": "start"})
    for pattern, action in _JARVIS:
        if pattern.match(cleaned):
            return ToolCall("jarvis_control", {"action": action})
    suggestion = _SUGGESTION.match(cleaned)
    if suggestion:
        return ToolCall("memory_suggestion", {"action": "reject" if suggestion.group("no") or cleaned.lower()
                                              .startswith("don") else "accept"})
    if _REVIEW.match(cleaned):
        return ToolCall("review_mistakes", {})
    fix = _ACCEPT_FIX.match(cleaned)
    if fix:
        return ToolCall("accept_fix", {"which": "all" if fix.group("all") else (fix.group("num") or "1").strip()})
    if _CLEAN_TEMP.match(cleaned):
        return ToolCall("clean_temp_files", {})
    setting = _SET.match(cleaned)
    if setting and known_setting is not None:
        name = setting.group("name") or setting.group("name2") or setting.group("name3")
        value = setting.group("state") or setting.group("state2") or setting.group("value3")
        if name and value and known_setting(name):
            return ToolCall("change_setting", {"setting": name.strip(), "value": value.strip()})
    from app.tools.voice_control import APP_COMMANDS

    word = bare_keyword(cleaned)
    if word and word not in BARE_ACTIONS:
        for group in ("browser", "youtube", "word", "code", "explorer", "any"):
            if word in APP_COMMANDS[group]:
                return ToolCall("app_command", {"command": word})
    return None


# "search Imran Hashmi on YouTube", "search YouTube for cats", "google the weather in Dhaka",
# "ইউটিউবে ইমরান হাশমি সার্চ করো"  (the AI once "typed" the search into Jarvis's own window instead)
_SEARCH_ON = re.compile(
    r"^(?:jarvis,?\s+)?(?:please\s+)?(?:search|look\s+up|find)\s+(?:for\s+)?(?P<query>.{2,120}?)\s+(?:on|in|at|using)\s+"
    r"(?P<engine>youtube|google)(?:\s+please)?[.!?]*$", re.I)
_SEARCH_ON_FIRST = re.compile(
    r"^(?:jarvis,?\s+)?(?:please\s+)?(?:search\s+(?P<engine>youtube|google)\s+(?:for\s+)?|"
    r"open\s+(?P<engine2>youtube|google)\s+and\s+search\s+(?:for\s+)?)(?P<query>.{2,120}?)(?:\s+please)?[.!?]*$", re.I)
_SEARCH_BN = re.compile(r"^(?P<engine>ইউটিউবে|ইউটিউব\s+এ|গুগলে|গুগল\s+এ)\s+(?P<query>.{2,120}?)\s+(?:সার্চ|খুঁজ)(?:\s+কর(?:ো|ে\s+দাও))?"
                        r"[।.!?]*$")

# "close everything", "close all apps", "close all windows except Chrome", "সব বন্ধ করো"
_CLOSE_ALL = re.compile(
    r"^(?:jarvis,?\s+)?(?:please\s+)?(?:close|quit|shut|exit)\s+(?:down\s+)?(?:everything|all(?:\s+(?:the|my|of\s+the))?"
    r"(?:\s+(?:open|running))?(?:\s+(?:apps?|applications?|programs?|windows?|tabs\s+and\s+apps))?|every\s+(?:app|window|program))"
    r"(?:\s+(?:that'?s|that\s+is|that\s+are)\s+open)?(?:\s+(?:except|but|apart\s+from)\s+(?P<keep>[\w\s,.&'-]{2,80}?))?"
    r"(?:\s+please)?[.!]*$", re.I)
_CLOSE_ALL_BN = re.compile(r"^(?:সব|সবকিছু|সব\s+(?:অ্যাপ|এপ|উইন্ডো|প্রোগ্রাম)(?:গুলো)?)\s+বন্ধ\s+(?:করো|করে\s+দাও|কর)[।.!]*$")

# "focus for 45 minutes", "start focus mode and close distractions", "stop focus", "how long is left"
_FOCUS_START = re.compile(
    r"^(?:jarvis,?\s+)?(?:(?:start|begin|turn\s+on)\s+(?:a\s+)?(?:focus(?:\s+mode)?|deep\s+work|pomodoro)|"
    r"(?:let\s+me\s+|i\s+(?:want|need)\s+to\s+)?focus|focus\s+mode|do\s+not\s+disturb(?:\s+mode)?)"
    r"(?:\s+(?:for|during)\s+(?P<amount>\d{1,3}|an?|one|two|half\s+an?)\s*(?P<unit>minutes?|mins?|hours?|hrs?))?"
    r"(?P<close>\s+(?:and|,)\s+close\s+(?:the\s+)?(?:distractions|distracting\s+apps|games|chat\s+apps))?"
    r"(?:\s+please)?[.!]*$", re.I)
_FOCUS_STOP = re.compile(r"^(?:jarvis,?\s+)?(?:stop|end|cancel|turn\s+off|finish)\s+(?:the\s+)?(?:focus(?:\s+mode)?|"
                         r"deep\s+work|pomodoro|do\s+not\s+disturb)(?:\s+please)?[.!]*$|^মনোযোগ\s+(?:মোড\s+)?বন্ধ\s+করো[।.!]*$",
                         re.I)
_FOCUS_STATUS = re.compile(r"^(?:how\s+(?:long|much\s+time)\s+(?:is\s+)?left\s+(?:on|in|of)\s+(?:my\s+|the\s+)?focus(?:\s+mode)?|"
                           r"how\s+long\s+(?:until|till)\s+focus\s+ends)\??$", re.I)
_FOCUS_BN = re.compile(r"^(?P<amount>[০-৯\d]{1,3})\s*(?P<unit>মিনিট|ঘণ্টা)\s+(?:মনোযোগ|ফোকাস)\s+(?:মোড\s+)?(?:চালু\s+করো|দাও)[।.!]*$")
_WORD_NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "half an": 0.5, "half a": 0.5}


def focus_command(text: str) -> ToolCall | None:
    if _FOCUS_STOP.match(text):
        return ToolCall("focus_mode", {"action": "stop"})
    if _FOCUS_STATUS.match(text):
        return ToolCall("focus_mode", {"action": "status"})
    match = _FOCUS_START.match(text) or _FOCUS_BN.match(text)
    if not match:
        return None
    minutes = 25
    amount = match.groupdict().get("amount")
    if amount:
        amount = amount.translate(str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")).lower()
        value = float(_WORD_NUMBERS.get(" ".join(amount.split()), 0) or amount) if not amount.isdigit() else float(amount)
        unit = (match.group("unit") or "").lower()
        minutes = int(value * 60) if unit.startswith(("h", "ঘ")) else int(value)
    arguments = {"action": "start", "minutes": minutes}
    if match.groupdict().get("close"):
        arguments["close_distractions"] = True
    return ToolCall("focus_mode", arguments)


# "what was I doing before lunch?", "what did I do yesterday", "what did you do for me this morning"
_RECALL = re.compile(
    r"^(?:jarvis,?\s+)?what\s+(?:was\s+i\s+doing|did\s+i\s+do|have\s+i\s+done|did\s+you\s+do(?:\s+for\s+me)?|"
    r"was\s+i\s+working\s+on)\s*(?P<period>(?:today|yesterday|this\s+\w+|last\s+[\w\s]+|before\s+lunch|"
    r"after\s+lunch|in\s+the\s+last\s+[\w\s]+)?)\s*\??$", re.I)
# "continue where I left off", "pick up where we left off", "restore my last session"
RESUME = re.compile(r"^(?:jarvis,?\s+)?(?:(?:let'?s\s+)?(?:continue|carry\s+on|pick\s+up|start)\s+(?:from\s+)?where\s+"
                    r"(?:i|we)\s+(?:left|stopped)(?:\s+off)?|(?:restore|reopen|resume)\s+(?:my\s+)?(?:last\s+)?"
                    r"(?:session|work))(?:\s+please)?[.!]*$|^(?:যেখানে|যেখান\s+থেকে)\s+(?:ছেড়েছিলাম|থেমেছিলাম)"
                    r".*$|^আগের\s+(?:কাজ|সেশন)\s+(?:আবার\s+)?(?:খোলো|শুরু\s+করো)[।.!]*$", re.I)
# "আজ আমি কী করেছি?", "গতকাল কী করেছিলাম"
_RECALL_BN = re.compile(r"^(?P<when>আজ|গতকাল|আজ\s+সকালে|সকালে|দুপুরের\s+আগে|কাল)?\s*(?:আমি\s+)?(?:কী|কি)\s+"
                        r"(?:করেছি|করেছিলাম|করছিলাম|করলাম)[?？।]*$")
_BN_PERIODS = {"আজ": "today", "গতকাল": "yesterday", "কাল": "yesterday", "আজ সকালে": "this morning",
               "সকালে": "this morning", "দুপুরের আগে": "before lunch"}

# "save this as my work setup", "remember this setup as study time"
SAVE_SETUP = re.compile(r"^(?:jarvis,?\s+)?(?:save|remember|keep)\s+(?:this|these|my\s+(?:current\s+)?setup|what\s+i(?:'ve|\s+have)?"
                        r"\s+opened|this\s+setup|these\s+apps)\s+as\s+(?:my\s+|the\s+|a\s+)?(?P<name>[\w\s'-]{2,40}?)"
                        r"(?:\s+(?:setup|routine|workflow))?(?:\s+please)?[.!]*$", re.I)

# "open chrome labib", "open chrome with the labib profile", "open edge profile 3"
_BROWSER_PROFILE = re.compile(
    r"^(?:please\s+)?(?:open|launch|start)\s+(?P<browser>google\s+chrome|chrome|microsoft\s+edge|edge|brave)\s+"
    r"(?:(?:with|in|on|using)\s+)?(?:(?:the|my)\s+)?(?P<profile>(?:profile\s+\d{1,3})|[\w.@-]+(?:\s+(?!profile\b)[\w.@-]+)?)"
    r"(?:\s+profile)?\s*(?:please)?[.!?]*$", re.I)
_NOT_A_PROFILE = re.compile(r"^(?:and|then|to|for|new|incognito|private|window|tab|browser|app|now|again|please|"
                            r"settings|history|downloads|bookmarks|extensions|update|dev|developer)\b",
                            re.I)


# ------------------------------------------------------------------ "volume set at 50", "brightness to 70"
#
# Any word order: "volume set at 50", "set the volume to 50%", "make volume fifty", "increase volume to 70",
# "volume up by 20", "brightness 30", "lower the brightness", "max brightness", "ভলিউম ৫০ করো".
# Every word must be one the parser knows, so a longer request ("set volume to 50 and open Chrome")
# still goes to the AI.

_LEVEL_SUBJECTS = {
    "volume": "volume", "sound": "volume", "audio": "volume", "speaker": "volume", "speakers": "volume",
    "ভলিউম": "volume", "ভলিউমটা": "volume", "সাউন্ড": "volume", "আওয়াজ": "volume", "শব্দ": "volume",
    "brightness": "brightness", "ব্রাইটনেস": "brightness", "ব্রাইটনেসটা": "brightness", "উজ্জ্বলতা": "brightness",
}
_LEVEL_UP = {"increase", "raise", "up", "boost", "higher", "louder", "brighter", "more", "বাড়াও", "বাড়ান",
             "বাড়িয়ে", "বেশি"}
_LEVEL_DOWN = {"decrease", "reduce", "lower", "down", "drop", "quieter", "softer", "dimmer", "dim", "less",
               "কমাও", "কমান", "কমিয়ে", "কম"}
_LEVEL_MAX = {"max", "maximum", "full", "highest", "fullest", "ফুল", "সর্বোচ্চ"}
_LEVEL_MIN = {"min", "minimum", "lowest", "সর্বনিম্ন"}
_LEVEL_ASK = {"what", "what's", "whats", "how", "much", "current", "currently", "tell", "me", "s", "check", "show", "কত",
              "কতো", "এখন"}
_LEVEL_FILLER = {
    "please", "jarvis", "sir", "set", "the", "my", "make", "put", "turn", "change", "adjust", "bring", "keep",
    "level", "to", "at", "on", "should", "be", "it", "is", "percent", "percentage", "per", "cent", "of", "a",
    "screen", "display", "monitor", "system", "pc", "computer", "laptop", "now", "can", "you", "could", "would",
    "and", "go", "get", "all", "way", "little", "bit", "value",
    "করো", "করুন", "কর", "দাও", "দিন", "সেট", "লেভেল", "পার্সেন্ট", "শতাংশ", "এ", "তে", "টা", "কে", "একটু",
    "স্ক্রিন", "স্ক্রিনের", "এর", "র", "পর্যন্ত", "প্লিজ", "জার্ভিস",
}
_ONES = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
         "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}
_BN_NUMBERS = {"শূন্য": 0, "দশ": 10, "বিশ": 20, "কুড়ি": 20, "ত্রিশ": 30, "চল্লিশ": 40, "পঞ্চাশ": 50,
               "ষাট": 60, "সত্তর": 70, "আশি": 80, "নব্বই": 90, "একশ": 100, "একশো": 100, "পঁচিশ": 25, "পঁচাত্তর": 75}
_BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")


def _nfc(words):
    return type(words)({unicodedata.normalize("NFC", w): v for w, v in words.items()}) if isinstance(words, dict) \
        else {unicodedata.normalize("NFC", w) for w in words}


_LEVEL_SUBJECTS, _LEVEL_UP, _LEVEL_DOWN, _LEVEL_FILLER, _BN_NUMBERS = (  # "বাড়াও" is typed two ways in Unicode
    _nfc(_LEVEL_SUBJECTS), _nfc(_LEVEL_UP), _nfc(_LEVEL_DOWN), _nfc(_LEVEL_FILLER), _nfc(_BN_NUMBERS))


def _level_tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFC", text).translate(_BN_DIGITS).lower().replace("%", " percent ")
    text = re.sub(r"(\d)([^\d\s])", r"\1 \2", text)  # "৫০এ" / "50-এ" -> "50 এ"
    text = re.sub(r"([^\d\s])(\d)", r"\1 \2", text)
    return [t for t in re.split(r"[\s,.!?।:;'\"-]+", text) if t]


def _numbers(tokens: list[str]) -> tuple[list[str], list[int]]:
    """Pull the numbers out: '50', 'fifty', 'fifty five', 'one hundred', 'half', 'পঞ্চাশ'."""
    rest: list[str] = []
    found: list[int] = []
    i = 0
    while i < len(tokens):
        word = tokens[i]
        if word.isdigit():
            found.append(int(word))
        elif word == "half":
            found.append(50)
        elif word in _BN_NUMBERS:
            found.append(_BN_NUMBERS[word])
        elif word in _TENS:
            value = _TENS[word]
            if i + 1 < len(tokens) and tokens[i + 1] in _ONES and 0 < _ONES[tokens[i + 1]] < 10:
                value += _ONES[tokens[i + 1]]
                i += 1
            found.append(value)
        elif word == "hundred" or (word in ("one", "a") and i + 1 < len(tokens) and tokens[i + 1] == "hundred"):
            found.append(100)
            i += 1 if word != "hundred" else 0
        elif word in _ONES:
            found.append(_ONES[word])
        else:
            rest.append(word)
        i += 1
    return rest, found


def level_command(text: str) -> ToolCall | None:
    """Volume / brightness to a number, up / down (optionally 'by N'), max / min; None if it's anything else."""
    tokens = _level_tokens(text)
    if not tokens or len(tokens) > 12:
        return None
    subjects = {_LEVEL_SUBJECTS[t] for t in tokens if t in _LEVEL_SUBJECTS}
    if len(subjects) != 1:
        return None
    subject = subjects.pop()
    rest, numbers = _numbers([t for t in tokens if t not in _LEVEL_SUBJECTS])
    by = "by" in rest or "বাই" in rest
    words = [t for t in rest if t not in _LEVEL_FILLER and t not in ("by", "বাই")]
    up = any(t in _LEVEL_UP for t in words)
    down = any(t in _LEVEL_DOWN for t in words)
    top = any(t in _LEVEL_MAX for t in words)
    bottom = any(t in _LEVEL_MIN for t in words)
    ask = any(t in _LEVEL_ASK for t in words)
    words = [t for t in words if t not in _LEVEL_ASK]
    if any(t not in _LEVEL_UP | _LEVEL_DOWN | _LEVEL_MAX | _LEVEL_MIN for t in words) or len(numbers) > 1:
        return None
    if (up and down) or (top and bottom) or (ask and numbers):
        return None
    tool = "volume_control" if subject == "volume" else "brightness_control"
    number = numbers[0] if numbers else None
    if number is not None and number > 100:
        return None
    if number is not None and by and (up or down):
        return ToolCall(tool, {"action": "up" if up else "down", "step": max(1, min(50, number))})
    if number is not None:
        return ToolCall(tool, {"action": "set", "level": number})
    if top or bottom:
        return ToolCall(tool, {"action": "set", "level": 100 if top else 0})
    if up or down:
        return ToolCall(tool, {"action": "up" if up else "down"})
    if not words and not by and (ask or len(tokens) <= 3):
        return ToolCall(tool, {"action": "get"})
    return None


# ------------------------------------------------------------------ 3.3: YouTube, layouts, dev, messages

_P = r"^(?:please\s+)?(?:jarvis\s*,?\s+)?"
_E = r"(?:\s+please)?[.!?]*$"

# "visualize my desktop", "look at my screen", "what's on my screen" -> Jarvis looks (describe_screen)
_LOOK_SCREEN = re.compile(_P + r"(?:visuali[sz]e[ds]?|look\s+at|describe|check|analy[sz]e|read)\s+(?:my|the|this)\s+"
                          r"(?:screen|desktop)|^(?:please\s+)?(?:jarvis\s*,?\s+)?(?:what'?s|what\s+is|what\s+do\s+you\s+"
                          r"see|what\s+can\s+you\s+see)\s+on\s+(?:my|the)\s+(?:screen|desktop)" + _E, re.I)
# "restart Spotify", "relaunch Chrome" (the PC words are power_control's)
_RESTART_APP = re.compile(_P + r"(?:restart|relaunch|reopen|reload)\s+(?:the\s+|my\s+)?(?P<name>[\w .+&'-]{2,40}?)"
                          r"(?:\s+app(?:lication)?)?(?:\s+for\s+me)?" + _E, re.I)
_NOT_RESTARTABLE = re.compile(r"^(?:pc|computer|laptop|system|device|machine|windows|jarvis|yourself|router|wi-?fi|"
                              r"internet|network|bluetooth|explorer|file\s+explorer|windows\s+explorer|it|that|this|"
                              r"everything|all|the\s+download|download|video|song|music|timer|mission|game)$", re.I)
# the last resort before the model: "open Cloud for me" -> open_application searches, suggests or offers a search
_OPEN_ANY = re.compile(_P + r"(?:open|launch)\s+(?:up\s+)?(?P<name>[\w.+&'-]+(?:\s+[\w.+&'-]+)?)(?:\s+for\s+me)?" + _E,
                       re.I)
_NOT_AN_APP_WORD = re.compile(r"^(?:a|an|the|my|new|last|latest|recent|it|that|this|them|file|folder|document|"
                              r"link|page|tab|window|email|mail|message|chat|photo|picture|image|video|song|project|"
                              r"report|downloads?|documents?|desktop|pictures?|music|videos?|settings|up)$", re.I)


# the evolution engine: "improve yourself", "test proposal 3", "roll back the last update"
_IMPROVE = re.compile(_P + r"(?:improve|upgrade|evolve|optimi[sz]e|fix)\s+yourself" + _E, re.I)
_PROPOSALS = re.compile(_P + r"(?:show|list|what\s+are)\s+(?:me\s+)?(?:your\s+|the\s+|my\s+)?(?:improvement\s+)?"
                        r"proposals" + _E, re.I)
_PROPOSAL_ACTION = re.compile(_P + r"(?P<verb>show|test|try|apply|reject)\s+(?:the\s+)?proposal\s+(?:number\s+|no\.?\s*|#)?"
                              r"(?P<n>\d{1,5})" + _E, re.I)
_ROLLBACK = re.compile(_P + r"(?:roll\s*back|undo)\s+(?:proposal\s+(?:number\s+|#)?(?P<n>\d{1,5})|(?:the|your|my)\s+"
                       r"(?:last\s+)?(?:self[- ]?)?(?:update|improvement|code\s+change))" + _E, re.I)
_EVOLUTION_CENTER = re.compile(_P + r"(?:open|show)\s+(?:the\s+)?evolution(?:\s+cent(?:er|re))?" + _E, re.I)


# 3.12: research, video summaries, interpreter, study
_RESEARCH = re.compile(_P + r"(?:do\s+(?:some\s+|a\s+)?)?research\s+(?:on\s+|about\s+|into\s+)?(?P<q>.{3,280}?)"
                       + _E, re.I)
_SUMMARY_VIDEO = re.compile(_P + r"(?:summari[sz]e|sum\s+up|give\s+me\s+(?:a\s+)?summary\s+of)\s+(?:this|the|that|"
                            r"current)?\s*(?:youtube\s+)?(?:video|song)?\s*(?P<url>(?:https?://)?(?:www\.|m\.)?"
                            r"(?:youtube\.com|youtu\.be)/\S+)?" + _E, re.I)
_INTERPRETER = re.compile(r"(?:" + _P + r"(?:start\s+|turn\s+on\s+)?(?:(?:interpreter|interpreting|translator|translation)\s+mode|"
                          r"be\s+my\s+(?:interpreter|translator)|translate\s+everything(?:\s+i\s+say)?|interpret"
                          r"(?:\s+for\s+me)?)(?:\s+(?:into|to|in)\s+(?P<lang>[a-z]+))?" + _E
                          + r"|^(?:অনুবাদক|দোভাষী)\s*(?:মোড)?\s*(?:চালু\s*করো)?[।.!]*$)", re.I)
_STUDY = re.compile(_P + r"(?:quiz\s+me|test\s+me|start\s+(?:a\s+)?(?:quiz|study\s+mode))\s+(?:on|about|in|for)\s+"
                    r"(?P<topic>.{2,140}?)" + _E, re.I)


_BRIGHT_CHECK = re.compile(_P + r"(?:check\s+(?:the\s+)?brightness|brightness\s+check|why\s+(?:can'?t|cannot|don'?t)\s+you\s+"
                           r"(?:change|set|control)\s+(?:the\s+|my\s+)?(?:monitor'?s?\s+)?brightness)" + _E, re.I)


_PROACTIVE = re.compile(_P + r"(?:(?P<more>be\s+more\s+proactive)|(?P<less>be\s+less\s+proactive)|(?P<off>stop\s+(?:making\s+)?"
                        r"suggest(?:ing|ions)(?:\s+things)?)|(?P<why>why\s+did\s+you\s+(?:suggest|say|tell\s+me)\s+"
                        r"that)|(?P<status>how\s+proactive\s+are\s+you))" + _E, re.I)
_QUIET = re.compile(_P + r"(?:set\s+)?quiet\s+hours\s+(?:from\s+|to\s+)?(?P<a>\d{1,2})(?::(?P<am>\d{2}))?\s*(?P<ap>am|pm)?"
                    r"\s+(?:to|until|-)\s+(?P<b>\d{1,2})(?::(?P<bm>\d{2}))?\s*(?P<bp>am|pm)?" + _E, re.I)


def _clock(hour: str, minute: str | None, half: str | None) -> str:
    value = int(hour) % 12 + (12 if (half or "").lower() == "pm" else 0) if half else int(hour)
    return f"{value % 24:02d}:{int(minute or 0):02d}"


_MODELS = re.compile(_P + r"(?:(?:which|what)\s+(?:ai\s+)?models?\s+(?:are\s+you\s+using|do\s+you\s+(?:use|have)|did\s+"
                     r"you\s+use|answered)|(?:show|list)\s+(?:the\s+|my\s+)?(?:ai\s+)?models)" + _E, re.I)
_CONTEXT = re.compile(_P + r"(?:what\s+am\s+i\s+(?:working\s+on|doing)(?:\s+(?:right\s+)?now)?|(?:show|what'?s|what\s+is)\s+"
                      r"(?:my\s+|the\s+)?(?:current\s+)?context|what\s+do\s+you\s+(?:understand|know)\s+about\s+what\s+"
                      r"i'?m\s+doing|where\s+(?:am\s+i|was\s+i)\s+(?:in\s+my\s+work|up\s+to))" + _E, re.I)


_KEEP_MEMORY = re.compile(_P + r"(?:keep\s+the\s+(?P<which>new|newer|old|older)\s+(?:one|memory|fact)|(?P<both>both\s+"
                          r"(?:are|of\s+them\s+are)\s+(?:true|right|correct)))" + _E, re.I)
_MEMORY_CONFLICTS = re.compile(_P + r"(?:any|show|list)\s+(?:my\s+)?memory\s+(?:conflicts|contradictions)" + _E, re.I)


def match_sessions_and_research(cleaned: str) -> ToolCall | None:
    from app.core import knowledge_graph as kg

    question = re.sub(r"^(?:please\s+)?(?:jarvis\s*,?\s+)?", "", cleaned, flags=re.I)
    if any(p.match(question) for p in (kg._FILES_Q, kg._DECISION_Q, kg._CONTAINS_Q, kg._WORKED_Q, kg._RELATED_Q,
                                         kg._BLOCKING_Q, kg._LEFT_Q, kg._FAILURES_Q, kg._STALE_Q)):
        return ToolCall("ask_knowledge", {"question": question})
    match = _KEEP_MEMORY.match(cleaned)
    if match:
        keep = "both" if match.group("both") else "new" if match.group("which").lower().startswith("new") else "old"
        return ToolCall("resolve_memory_conflict", {"keep": keep})
    if _MEMORY_CONFLICTS.match(cleaned):
        return ToolCall("memory_conflicts", {})
    if _CONTEXT.match(cleaned):
        return ToolCall("current_context", {})
    if _MODELS.match(cleaned):
        return ToolCall("model_status", {})
    match = _PROACTIVE.match(cleaned)
    if match:
        if match.group("off"):
            return ToolCall("proactive_control", {"action": "set_level", "level": "off"})
        action = next(k for k in ("more", "less", "why", "status") if match.group(k))
        return ToolCall("proactive_control", {"action": action})
    match = _QUIET.match(cleaned)
    if match:
        hours = f"{_clock(match.group('a'), match.group('am'), match.group('ap'))}-" \
                f"{_clock(match.group('b'), match.group('bm'), match.group('bp'))}"
        return ToolCall("proactive_control", {"action": "quiet_hours", "hours": hours})
    if _BRIGHT_CHECK.match(cleaned):
        return ToolCall("brightness_control", {"action": "check"})
    match = _SUMMARY_VIDEO.match(cleaned)
    if match and (match.group("url") or re.search(r"\b(?:video|song)\b", cleaned, re.I)):
        return ToolCall("summarize_video", {"url": match.group("url")} if match.group("url") else {})
    match = _INTERPRETER.match(cleaned)
    if match:
        language = match.groupdict().get("lang")
        return ToolCall("interpreter_mode", {"action": "start", **({"language": language} if language else {})})
    match = _STUDY.match(cleaned)
    if match:
        return ToolCall("study_mode", {"action": "start", "topic": match.group("topic").strip(" ,.")})
    match = _RESEARCH.match(cleaned)
    if match and not re.match(r"^(?:yourself|it|that|this)$", match.group("q").strip(), re.I):
        return ToolCall("research", {"question": match.group("q").strip(" ,.?")})
    return None


_VERSIONS = re.compile(_P + r"(?:show|list)\s+(?:me\s+)?(?:the\s+)?(?:old\s+|earlier\s+)?versions\s+of\s+(?:the\s+|my\s+)?"
                       r"(?P<name>.{2,60}?)(?:\s+(?:workflow|automation))?" + _E, re.I)
_RESTORE_VERSION = re.compile(_P + r"(?:restore|bring\s+back)\s+version\s+(?P<n>\d{1,4})\s+of\s+(?:the\s+|my\s+)?"
                              r"(?P<name>.{2,60}?)(?:\s+(?:workflow|automation))?" + _E, re.I)


def match_evolution(cleaned: str) -> ToolCall | None:
    match = _RESTORE_VERSION.match(cleaned)
    if match:
        return ToolCall("restore_workflow", {"name": match.group("name").strip(" ,."), "version": int(match.group("n"))})
    match = _VERSIONS.match(cleaned)
    if match:
        return ToolCall("workflow_versions", {"name": match.group("name").strip(" ,.")})
    if _IMPROVE.match(cleaned):
        return ToolCall("improve_yourself", {})
    if _PROPOSALS.match(cleaned):
        return ToolCall("list_proposals", {})
    if _EVOLUTION_CENTER.match(cleaned):
        return ToolCall("jarvis_control", {"action": "evolution_center"})
    match = _PROPOSAL_ACTION.match(cleaned)
    if match:
        verb = match.group("verb").lower()
        tool = {"show": "show_proposal", "test": "test_proposal", "try": "test_proposal", "apply": "apply_proposal",
                "reject": "reject_proposal"}[verb]
        return ToolCall(tool, {"id": int(match.group("n"))})
    match = _ROLLBACK.match(cleaned)
    if match:
        return ToolCall("rollback_proposal", {"id": int(match.group("n"))} if match.group("n") else {})
    return None


def match_open_any(cleaned: str) -> ToolCall | None:
    match = _OPEN_ANY.match(cleaned)
    if not match:
        return None
    name = match.group("name").strip(" ,.")
    words = name.split()
    if not words or any(_NOT_AN_APP_WORD.match(word) for word in words) or "." in name:
        return None
    return ToolCall("open_application", {"name": name})
_YT_SKIP_AD = re.compile(_P + r"skip\s+(?:the\s+|this\s+)?ads?" + _E, re.I)
_YT_LIKE = re.compile(_P + r"like\s+(?:this|the)\s+(?:song|video)" + _E, re.I)
_YT_SEEK = re.compile(_P + r"(?:(?:skip|go|jump|fast)\s+)?(?P<dir>forward|ahead|back(?:ward)?s?|rewind|skip)"
                      r"\s+(?:by\s+)?(?P<n>[\w-]+(?:\s+[\w]+)?)\s+(?P<unit>seconds?|secs?|minutes?|mins?)" + _E, re.I)
_YT_RESTART = re.compile(_P + r"(?:(?:start|play)\s+(?:the\s+|this\s+)?(?:song|video)\s+(?:again\s+)?from\s+the\s+"
                         r"(?:start|beginning)|(?:start|play)\s+(?:the\s+|this\s+)?(?:song|video)\s+over|restart\s+"
                         r"(?:the\s+|this\s+)?(?:song|video)|start\s+(?:the\s+|this\s+)?(?:song|video)\s+again)" + _E, re.I)
_YT_NOW = re.compile(_P + r"(?:(?:what|which)\s+(?:song|video|music)\s+is\s+(?:this|playing)(?:\s+now)?|what(?:'s|\s+is)\s+"
                     r"(?:playing|this\s+song)(?:\s+now)?|(?:what(?:'s|\s+is)\s+)?the\s+name\s+of\s+this\s+song|"
                     r"what\s+am\s+i\s+listening\s+to)" + _E, re.I)
_YT_QUEUE = re.compile(_P + r"(?:(?:after\s+this(?:\s+song)?|next)\s*,?\s+play\s+(?P<q>.+?)|play\s+(?P<q2>.+?)\s+"
                       r"(?:next|after\s+this(?:\s+(?:song|one))?)|queue\s+(?:up\s+)?(?P<q3>.+?)|add\s+(?P<q4>.+?)\s+to\s+"
                       r"(?:the\s+|my\s+)?(?:queue|playlist))" + _E, re.I)
_YT_SHOW_QUEUE = re.compile(_P + r"(?:what(?:'s|\s+is)\s+(?:in\s+)?(?:the\s+|my\s+)?queue|(?:show|list)\s+(?:me\s+)?"
                            r"(?:the\s+|my\s+)?queue|what(?:'s|\s+is)\s+playing\s+next|what\s+(?:song\s+)?(?:plays|is\s+playing)\s+next|"
                            r"which\s+song\s+is\s+next)" + _E, re.I)
_YT_CLEAR = re.compile(_P + r"(?:clear|empty)\s+(?:the\s+|my\s+)?queue" + _E, re.I)
_YT_REPEAT = re.compile(_P + r"(?:play\s+(?:this|it|the\s+song|this\s+song)\s+(?:again|one\s+more\s+time)|repeat\s+"
                        r"(?:this|the|it)(?:\s+(?:song|video|one))?|repeat\s+(?:the\s+)?song|once\s+more)" + _E, re.I)
_YT_CAPTIONS = re.compile(_P + r"(?:(?:turn\s+|switch\s+)?(?:the\s+)?(?:captions|subtitles)\s+(?:on|off)|(?:turn|switch)\s+"
                          r"(?:on|off)\s+(?:the\s+)?(?:captions|subtitles)|(?:show|hide)\s+(?:the\s+)?(?:captions|subtitles))"
                          + _E, re.I)
_YT_FULL = re.compile(_P + r"(?:(?:make\s+)?(?:the\s+|this\s+)?video\s+full\s*screen|full\s*screen\s+(?:the\s+)?video|"
                      r"exit\s+full\s*screen\s+video)" + _E, re.I)
_YT_MUTE = re.compile(_P + r"(?:un)?mute\s+(?:the\s+|this\s+)?video" + _E, re.I)

_WHERE = {"left": "left", "right": "right", "top": "top", "bottom": "bottom", "top left": "top_left",
          "top right": "top_right", "bottom left": "bottom_left", "bottom right": "bottom_right",
          "middle": "center", "center": "center", "centre": "center"}
_PLACE = re.compile(_P + r"(?:put|move|snap|place|send|throw)\s+(?P<app>.+?)\s+(?:(?:to|on|at|in)\s+(?:the\s+)?)?"
                    r"(?P<where>top\s+left|top\s+right|bottom\s+left|bottom\s+right|left|right|top|bottom|middle|"
                    r"center|centre)(?:\s+(?:side|half|corner|of\s+(?:the|my)\s+screen))?" + _E, re.I)
_SIDE = re.compile(_P + r"(?:put\s+)?(?P<a>.+?)\s+(?:on\s+the\s+|to\s+the\s+)?left\s*(?:,|and)\s*(?P<b>.+?)\s+"
                   r"(?:on\s+the\s+|to\s+the\s+)?right" + _E + r"|" + _P + r"(?:put\s+)?(?P<a2>.+?)\s+and\s+(?P<b2>.+?)"
                   r"\s+side\s+by\s+side" + _E, re.I)
_NEXT_SCREEN = re.compile(_P + r"(?:move|send|put|throw)\s+(?P<app>.+?)\s+to\s+(?:my\s+|the\s+)?(?:second|other|next|"
                          r"2nd|another)\s+(?:screen|monitor|display)" + _E, re.I)
_MAX_ALL = re.compile(_P + r"maximi[sz]e\s+(?:everything|all(?:\s+(?:the\s+)?(?:windows|apps))?)" + _E, re.I)
_LAYOUT_SAVE = re.compile(_P + r"save\s+(?:this|the|my|current)?\s*(?:window\s+)?(?:layout|setup\s+layout)\s+(?:as\s+)?"
                          r"(?P<n>[\w' -]{1,30}?)" + _E + r"|" + _P + r"save\s+(?:this|it|the\s+windows)\s+as\s+"
                          r"(?:my\s+|the\s+)?(?P<n2>[\w' -]{1,30}?)\s+layout" + _E, re.I)
_LAYOUT_LIST = re.compile(_P + r"(?:(?:list|show)\s+(?:me\s+)?(?:my\s+|the\s+)?(?:saved\s+)?layouts|what\s+layouts\s+"
                          r"(?:do\s+i\s+have|are\s+there))" + _E, re.I)
_LAYOUT_DELETE = re.compile(_P + r"(?:delete|remove|forget)\s+(?:my\s+|the\s+)?(?P<n>[\w' -]{1,30}?)\s+layout" + _E, re.I)
_LAYOUT_RESTORE = re.compile(_P + r"(?:(?:restore|load|apply|use|open|switch\s+to|go\s+to|set\s+up|bring\s+back)\s+)?"
                             r"(?:my\s+|the\s+)?(?P<n>[\w' -]{1,30}?)\s+layout" + _E, re.I)
_RUN_PROJECT = re.compile(_P + r"(?:run|start|launch|serve)\s+(?:my|the|this)\s+(?:project|app|website|site|server)"
                          + _E, re.I)
_COMMIT = re.compile(_P + r"commit\s+(?:my\s+|the\s+|all\s+(?:the\s+)?)?(?:changes|code|work|everything|it)?\s*"
                     r"(?:with\s+(?:the\s+)?(?:message|msg|note)|saying|as|message)\s+[\"']?(?P<m>.+?)[\"']?"
                     r"(?P<push>\s+and\s+push(?:\s+it)?)?" + _E, re.I)
_EXPLAIN_ERROR = re.compile(_P + r"(?:(?:what(?:'s|\s+is)|explain)\s+(?:this|that|the)\s+error(?:\s+on\s+(?:my\s+|the\s+)?"
                            r"screen)?|what\s+does\s+(?:this|that|the)\s+error\s+mean|how\s+(?:do\s+i|to)\s+fix\s+"
                            r"(?:this|that)\s+error|why\s+(?:is\s+(?:it|my\s+code)\s+(?:failing|crashing)|did\s+(?:it|this)"
                            r"\s+fail))" + _E, re.I)
_WHATSAPP_READ = re.compile(_P + r"(?:(?:any|do\s+i\s+have\s+(?:any\s+)?)\s*(?:new\s+|unread\s+)?(?:whatsapp|whats\s+app)\s+"
                       r"(?:messages?|texts?)|(?:read|check)\s+(?:my\s+)?(?:new\s+|unread\s+)?(?:whatsapp|whats\s+app)"
                       r"(?:\s+(?:messages?|chats?))?|(?:who|anyone)\s+(?:texted|messaged)\s+me\s+on\s+whatsapp)" + _E, re.I)
_EMAILS = re.compile(_P + r"(?:(?:read|check|summari[sz]e)\s+(?:my\s+)?(?:new\s+|unread\s+|latest\s+)?(?:e-?mails?|inbox)|"
                     r"any\s+new\s+e-?mails?|do\s+i\s+have\s+(?:any\s+)?(?:new\s+|unread\s+)?e-?mails?)" + _E, re.I)
_UNDO_JARVIS = re.compile(_P + r"(?:undo\s+(?:that|it|this)(?:\s+jarvis)?|undo\s+(?:your|the)\s+last\s+(?:action|change|thing|step)|"
                          r"undo\s+what\s+you\s+(?:just\s+)?did|revert\s+(?:that|your\s+last\s+(?:action|change))|"
                          r"take\s+that\s+back|put\s+it\s+back|আগের\s+কাজটা?\s+(?:ফিরিয়ে\s+দাও|বাতিল\s+করো)|আনডু\s+করো)" + _E,
                          re.I)
_UNDO_LIST = re.compile(_P + r"(?:what\s+can\s+you\s+undo|what\s+did\s+you\s+change)" + _E, re.I)
_GOAL = re.compile(_P + r"(?:my\s+(?:new\s+)?goal\s+is\s+(?:to\s+)?|set\s+(?:a\s+|my\s+)?(?:new\s+)?goal\s*(?:to|:)?\s*|"
                   r"new\s+goal\s*:?\s*)(?P<goal>.+?)" + _E, re.I)
_GOAL_DEADLINE = re.compile(r"\s+(?P<d>(?:by|before)\s+.+|this\s+(?:week|month|year)|next\s+(?:week|month)|today|tomorrow|"
                            r"in\s+(?:\d{1,3}|a|one|two|three|four)\s+(?:days?|weeks?|months?))$", re.I)
_GOALS_LIST = re.compile(_P + r"(?:what\s+are\s+my\s+goals|(?:show|list)\s+(?:me\s+)?my\s+goals|how\s+are\s+my\s+goals"
                         r"(?:\s+going)?|my\s+goals)" + _E, re.I)
_MISSION_RESUME = re.compile(_P + r"(?:continue|resume|run|retry|restart)\s+(?:my\s+|the\s+)?(?:last\s+)?mission" + _E, re.I)
_MISSION_SKIP = re.compile(_P + r"skip\s+(?:that|this|the\s+failed)\s+step(?:\s+and\s+continue)?" + _E, re.I)
_EXPLAIN = re.compile(_P + r"(?:why\s+did\s+you\s+(?:do\s+(?:that|this|it)|ask(?:\s+me)?(?:\s+(?:that|first))?)"
                      r"|why\s+didn'?t\s+you\s+do\s+(?:that|it)|what\s+did\s+you\s+(?:just\s+)?do"
                      r"|explain\s+(?:your\s+)?last\s+action)" + _E, re.I)
_DIAGNOSE = re.compile(_P + r"(?:diagnose\s+yourself|check\s+your(?:self|\s+health)|run\s+(?:a\s+)?diagnostics?)" + _E,
                       re.I)
_REPAIR = re.compile(_P + r"(?:repair|fix)\s+(?:yourself|jarvis)" + _E, re.I)
_LIST_AGENTS = re.compile(_P + r"(?:(?:list|show|what)\s+(?:are\s+)?(?:my\s+|the\s+)?agents?"
                          r"|who(?:'s|\s+is)\s+on\s+(?:my\s+|the\s+)?team)" + _E, re.I)
_MISSION_ROLLBACK = re.compile(_P + r"(?:roll\s*back|undo)\s+(?:my\s+|the\s+)?(?:last\s+)?mission" + _E, re.I)
_TASK_CENTER = re.compile(_P + r"(?:(?:show|list|open)\s+(?:me\s+)?(?:the\s+|my\s+)?(?:background\s+tasks|"
                          r"task\s+cent(?:er|re))|what(?:'s|\s+is)\s+running\s+in\s+the\s+background)" + _E, re.I)
_MY_DAY = re.compile(_P + r"(?:(?:show|open)\s+(?:me\s+)?my\s+(?:day|to-?do\s+(?:window|screen)|planner)|my\s+day)" + _E, re.I)
_DAY_STATUS = re.compile(_P + r"(?:how(?:'s|\s+is)\s+my\s+day(?:\s+going)?|what(?:'s|\s+is)\s+on\s+my\s+plate|"
                         r"(?:what\s+did\s+i|summari[sz]e\s+what\s+i)\s+(?:complete|completed|finish|finished|do|did)"
                         r"(?:\s+today)?|end\s+of\s+day\s+summary)" + _E, re.I)
_PC_HEALTH = re.compile(_P + r"(?:how(?:'s|\s+is)\s+my\s+(?:pc|computer|laptop)(?:\s+(?:doing|running|health))?|"
                        r"(?:is\s+)?(?:anything|something)\s+wrong\s+with\s+my\s+(?:pc|computer|laptop)|"
                        r"(?:pc|computer)\s+health(?:\s+report)?)" + _E, re.I)
_MISSION_CANCEL = re.compile(_P + r"(?:cancel|stop|abort)\s+(?:my\s+|the\s+)?mission" + _E, re.I)
_MISSION_STATUS = re.compile(_P + r"(?:mission\s+status|how(?:'s|\s+is)\s+(?:my\s+|the\s+)?mission(?:\s+going)?|"
                             r"what(?:'s|\s+is)\s+the\s+mission\s+status)" + _E, re.I)
_REMEMBER = re.compile(_P + r"remember\s+(?:(?P<private>privately|in\s+private|secretly)\s+|(?:for\s+)?(?P<until>today|tonight|"
                       r"this\s+week|until\s+[\w ]{3,20}?)\s+)?that\s+(?P<fact>.{3,300}?)" + _E, re.I)
_MEM_WHY = re.compile(_P + r"(?:why\s+do\s+you\s+remember\s+(?P<a>.+?)|where\s+did\s+you\s+(?:learn|get|hear)\s+(?:about\s+)?"
                      r"(?P<b>.+?)|where\s+did\s+(?:that|this)\s+(?:memory\s+)?come\s+from|how\s+do\s+you\s+know\s+"
                      r"that\s+about\s+me)" + _E, re.I)
_MEM_OFF = re.compile(_P + r"(?:stop\s+using|don'?t\s+use)\s+(?:that|this|the)\s+memory(?:\s+about\s+(?P<what>.+?))?" + _E, re.I)
_MEM_PRIVATE = re.compile(_P + r"(?:keep|make)\s+(?:that|this|it)(?:\s+memory)?\s+private" + _E, re.I)
_MEM_HISTORY = re.compile(_P + r"(?:how\s+did\s+(?:that|this)\s+memory\s+change|(?:show\s+)?(?:that\s+)?memory'?s?\s+history)" + _E,
                          re.I)
_WHAT_CAN = re.compile(_P + r"(?:what\s+can\s+you\s+do|what\s+are\s+your\s+(?:capabilities|abilities|skills)|"
                       r"what\s+tools\s+do\s+you\s+have)" + _E, re.I)
_WHAT_KNOW = re.compile(_P + r"what\s+do\s+you\s+know\s+about\s+(?:my\s+)?(?P<about>projects?|goals?|apps?|contacts|people|"
                        r"layouts?|nicknames?|me|my\s+computer|my\s+pc|my\s+setup)" + _E, re.I)
_ABOUT = {"project": "project", "projects": "project", "goal": "goal", "goals": "goal", "app": "app", "apps": "app",
          "contacts": "person", "people": "person", "layout": "layout", "layouts": "layout", "nickname": "nickname",
          "nicknames": "nickname"}
_TOGGLE_WHAT = r"(?P<what>wi-?fi|wifi|wi\s+fi|wireless|bluetooth|dark\s+mode|dark\s+theme|ওয়াইফাই|ব্লুটুথ|ডার্ক\s+মোড)"
_TOGGLE = re.compile(_P + r"(?:(?:turn|switch|put)\s+(?P<s1>on|off)\s+(?:the\s+)?" + _TOGGLE_WHAT.replace("what", "w1") +
                     r"|(?:turn|switch|put)\s+(?:the\s+)?" + _TOGGLE_WHAT.replace("what", "w2") + r"\s+(?P<s2>on|off)|"
                     + _TOGGLE_WHAT.replace("what", "w3") + r"\s+(?P<s3>on|off|চালু\s+করো|বন্ধ\s+করো|অন\s+করো|অফ\s+করো)|"
                     r"(?P<s4>enable|disable)\s+(?:the\s+)?" + _TOGGLE_WHAT.replace("what", "w4") + r"|(?:is\s+(?:the\s+|my\s+)?)"
                     + _TOGGLE_WHAT.replace("what", "w5") + r"\s+(?P<s5>on)\s*\??|(?P<light>light\s+mode\s+on|turn\s+on\s+light\s+mode|"
                     r"switch\s+to\s+light\s+mode))" + _E, re.I)
_PRESS_NAMED = re.compile(_P + r"(?:press|click|hit|tap)\s+(?:on\s+)?(?:the\s+)?(?P<name>[\w' .-]{1,40}?)\s+(?:button|link|tab|"
                          r"menu(?:\s+item)?|option|check\s*box)" + _E, re.I)
_PRESS_COMMON = re.compile(_P + r"(?:press|click|hit|tap)\s+(?:on\s+)?(?P<name>ok|okay|cancel|save|yes|no|next|back|finish|apply|"
                           r"close|continue|submit|send|done|install|allow|accept|sign\s+in|log\s+in|login|retry|skip|"
                           r"agree|i\s+agree|got\s+it|update|restart\s+now|not\s+now|later)" + _E, re.I)
_TICK = re.compile(_P + r"(?:tick|untick|un-tick)\s+(?:the\s+)?(?P<name>[\w' .-]{2,40}?)(?:\s+(?:box|check\s*box))?" + _E, re.I)
_FILE_ZIP = re.compile(_P + r"(?:zip|compress)\s+(?:up\s+)?(?:the\s+|my\s+)?(?P<what>.+?)(?:\s+folder)?(?:\s+into\s+a\s+zip)?" + _E,
                       re.I)
_FILE_UNZIP = re.compile(_P + r"(?:unzip|extract|unpack)\s+(?:the\s+|my\s+)?(?P<what>.+?\.zip)" + _E, re.I)
_RECORD = re.compile(_P + r"(?:(?P<act>start|stop|end|finish)\s+(?:the\s+)?(?:screen\s+)?recording(?:\s+(?:my|the)\s+screen)?|"
                     r"(?P<rec>record)\s+(?:my|the|this)\s+(?:screen|window|app))" + _E, re.I)
_DEV_PROFILE = re.compile(_P + r"(?:(?:tell\s+me\s+about|describe|analy[sz]e)\s+my\s+project|project\s+profile|what(?:'s|\s+is)\s+"
                          r"my\s+project\s+(?:like|built\s+with|made\s+of))" + _E, re.I)
_DEV_TEST = re.compile(_P + r"(?:(?:run|check)\s+(?:the\s+|my\s+)?(?:project'?s?\s+)?tests?|test\s+my\s+(?:project|code)|"
                       r"do\s+the\s+tests\s+pass)" + _E, re.I)
_DEV_BUILD = re.compile(_P + r"build\s+(?:my|the)\s+project" + _E, re.I)
_DEV_SEARCH = re.compile(_P + r"(?:(?:search|look)\s+(?:in\s+)?my\s+(?:code|project)\s+for\s+(?P<q1>.+?)|where\s+is\s+(?P<q2>.+?)\s+in\s+"
                         r"my\s+(?:code|project)|find\s+(?P<q3>.+?)\s+in\s+my\s+(?:code|project))" + _E, re.I)
_DEV_NOTE = re.compile(_P + r"(?:add\s+a\s+known\s+problem|note\s+a\s+(?:bug|problem))\s*:?\s+(?P<note>.{3,200}?)" + _E, re.I)
_NOTICES = re.compile(_P + r"(?:(?P<list>any\s+(?:new\s+)?notifications|(?:read|show)\s+(?:me\s+)?my\s+notifications|what(?:'s|\s+is)\s+new|"
                      r"what\s+did\s+i\s+miss)|(?P<clear>clear|dismiss)\s+(?:all\s+)?(?:my\s+|the\s+)?notifications|"
                      r"(?P<read>mark)\s+(?:all\s+)?(?:my\s+)?notifications\s+(?:as\s+)?read)" + _E, re.I)
_MODE = re.compile(_P + r"(?:(?:switch\s+to|turn\s+on|enable|go\s+(?:to|into)|start|enter|use)\s+)?(?P<mode>normal|work|coding|"
                   r"programming|research|study|gaming|game|presentation|meeting|private|privacy|incognito|maintenance)"
                   r"\s+mode" + _E, re.I)
_MODE_STATUS = re.compile(_P + r"(?:what|which)\s+mode\s+(?:are\s+you\s+in|is\s+(?:it|on))" + _E, re.I)
_EMERGENCY = re.compile(_P + r"(?:(?P<stop>emergency\s+stop|stop\s+everything(?:\s+now)?|abort\s+everything)|(?P<lock>lock\s+"
                        r"(?:yourself|jarvis))|(?P<net>(?:disable|turn\s+off)\s+(?:all\s+)?network\s+actions)|(?P<remote>"
                        r"(?:disable|turn\s+off)\s+(?:the\s+)?phone\s+remote)|(?P<auto>(?:disable|turn\s+off|stop)\s+(?:all\s+)?"
                        r"(?:my\s+)?automations))" + _E, re.I)
_DEV_CAN_WAKE = re.compile(_P + r"(?:can\s+you\s+(?:turn\s+on|wake(?:\s+up)?|start|power\s+on)\s+(?:my\s+|this\s+)?(?:laptop|pc|computer)"
                           r"(?:\s+remotely|\s+from\s+my\s+phone)?|how\s+(?:can|do)\s+i\s+(?:wake|turn\s+on|start)\s+(?:this|my)\s+"
                           r"(?:laptop|pc|computer)\s+(?:remotely|from\s+my\s+phone)|what\s+can\s+(?:my|this)\s+(?:laptop|pc|computer)"
                           r"\s+do|(?:this|my)\s+(?:pc|laptop|computer)'?s?\s+capabilities)" + _E, re.I)
_DEV_LIST = re.compile(_P + r"(?:(?:list|show)\s+(?:me\s+)?my\s+devices|my\s+devices|what\s+devices\s+do\s+i\s+have)" + _E, re.I)
_DEV_WAKE = re.compile(_P + r"(?:wake(?:\s+up)?|turn\s+on|power\s+on|start\s+up)\s+(?:my\s+|the\s+)?(?P<name>[\w' -]{2,40}?)"
                       r"(?:\s+(?:pc|computer))?(?:\s+remotely)?" + _E, re.I)
_DEV_CHECK = re.compile(_P + r"is\s+(?:my\s+|the\s+)?(?P<name>[\w' -]{2,40}?)\s+(?:on|online|awake|up|running)\s*\??" + _E, re.I)
_HIBERNATE = re.compile(_P + r"hibernate(?:\s+(?:my|the|this)\s+(?:pc|laptop|computer))?" + _E, re.I)
_THIS_WINDOW_WORDS = {"this", "this window", "it", "that", "that window", "the window", "current window", "this app",
                "my window"}


def _seconds(amount: str, unit: str) -> int | None:
    words = amount.lower().replace("-", " ").split()
    if words in (["a"], ["an"], ["one"]):
        value = 1
    else:
        rest, numbers = _numbers(words)
        if rest or len(numbers) != 1:
            return None
        value = numbers[0]
    value *= 60 if unit.lower().startswith("m") else 1
    return value if 1 <= value <= 600 else None


def _app_or_this(text: str, known_app: Callable[[str], str | None]) -> str | None | bool:
    """None = this window, a name = a known app, False = not something we can place."""
    name = " ".join(text.lower().split()).removeprefix("the ").strip()
    if name in _THIS_WINDOW_WORDS:
        return None
    return name if known_app(name) else False


def match_devices(cleaned: str, known_device: Callable[[str], bool] | None = None) -> ToolCall | None:
    if _DEV_CAN_WAKE.match(cleaned):
        return ToolCall("device_info", {})
    if _DEV_LIST.match(cleaned):
        return ToolCall("list_devices", {})
    if _HIBERNATE.match(cleaned):
        return ToolCall("power_control", {"action": "hibernate"})
    for pattern, tool in ((_DEV_WAKE, "wake_device"), (_DEV_CHECK, "check_device")):
        match = pattern.match(cleaned)
        if match and known_device is not None and known_device(match.group("name").strip()):
            return ToolCall(tool, {"name": match.group("name").strip()})
    return None


def match_proactive(cleaned: str) -> ToolCall | None:
    match = _EMERGENCY.match(cleaned)
    if match:
        action = ("stop" if match.group("stop") else "lock" if match.group("lock") else "disable_network"
                  if match.group("net") else "disable_remote" if match.group("remote") else "disable_automations")
        return ToolCall("emergency_control", {"action": action})
    match = _NOTICES.match(cleaned)
    if match:
        action = "clear" if match.group("clear") else "read_all" if match.group("read") else "list"
        return ToolCall("notifications", {"action": action})
    match = _MODE.match(cleaned)
    if match:
        return ToolCall("set_mode", {"mode": match.group("mode").lower()})
    if _MODE_STATUS.match(cleaned):
        return ToolCall("set_mode", {"mode": "status"})
    return None


def match_developer(cleaned: str) -> ToolCall | None:
    proactive = match_proactive(cleaned)
    if proactive is not None:
        return proactive
    if _DEV_PROFILE.match(cleaned):
        return ToolCall("project_profile", {})
    if _DEV_TEST.match(cleaned):
        return ToolCall("check_project", {"what": "test"})
    if _DEV_BUILD.match(cleaned):
        return ToolCall("check_project", {"what": "build"})
    match = _DEV_SEARCH.match(cleaned)
    if match:
        query = next(q for q in (match.group("q1"), match.group("q2"), match.group("q3")) if q).strip(" \"'.")
        if len(query) >= 2:
            return ToolCall("search_code", {"query": query})
    match = _DEV_NOTE.match(cleaned)
    if match:
        return ToolCall("add_project_note", {"note": match.group("note").strip()})
    return None


def match_controls_and_files(cleaned: str) -> ToolCall | None:
    developer = match_developer(cleaned)
    if developer is not None:
        return developer
    record = _RECORD.match(cleaned)
    if record:
        start = bool(record.group("rec")) or (record.group("act") or "").lower() == "start"
        return ToolCall("screen_recording", {"action": "start" if start else "stop"})
    match = _PRESS_NAMED.match(cleaned) or _PRESS_COMMON.match(cleaned) or _TICK.match(cleaned)
    if match:
        return ToolCall("press_control", {"name": " ".join(match.group("name").split())})
    match = _FILE_UNZIP.match(cleaned)
    if match:
        return ToolCall("extract_zip", {"path": match.group("what").strip()})
    match = _FILE_ZIP.match(cleaned)
    if match and not re.search(r"\b(photos?|pictures?|images?)\b", match.group("what"), re.I) or (
            match and re.search(r"\bfolder\b", cleaned, re.I)):
        return ToolCall("zip_items", {"paths": [match.group("what").strip()]})
    return None


def match_toggle(cleaned: str) -> ToolCall | None:
    handled = match_controls_and_files(cleaned)
    if handled is not None:
        return handled
    match = _TOGGLE.match(cleaned)
    if not match:
        return None
    if match.group("light"):
        return ToolCall("device_toggle", {"setting": "dark_mode", "state": "off"})
    what = next(match.group(f"w{i}") for i in range(1, 6) if match.group(f"w{i}")).lower()
    word = next(match.group(f"s{i}") for i in range(1, 6) if match.group(f"s{i}")).lower()
    setting = "bluetooth" if "blue" in what or "ব্লু" in what else "dark_mode" if "dark" in what or "ডার্ক" in what \
        else "wifi"
    if match.group("s5"):
        state = "status"
    else:
        state = "on" if word in ("on", "enable") or "চালু" in word or "অন" in word else "off"
    return ToolCall("device_toggle", {"setting": setting, "state": state})


def match_memory(cleaned: str) -> ToolCall | None:
    toggle = match_toggle(cleaned)
    if toggle is not None:
        return toggle
    if _WHAT_CAN.match(cleaned):
        return ToolCall("what_can_you_do", {})
    match = _WHAT_KNOW.match(cleaned)
    if match:
        about = _ABOUT.get(match.group("about").lower(), "everything")
        return ToolCall("what_do_you_know", {"about": about})
    match = _REMEMBER.match(cleaned)
    if match:
        arguments: dict = {"fact": match.group("fact").strip(" .")}
        if match.group("private"):
            arguments["private"] = True
        if match.group("until"):
            arguments["until"] = match.group("until").lower().removeprefix("until ").strip()
        return ToolCall("remember", arguments)
    match = _MEM_WHY.match(cleaned)
    if match:
        what = (match.group("a") or match.group("b") or "that").strip(" ?.")
        return ToolCall("explain_memory", {"what": what})
    match = _MEM_OFF.match(cleaned)
    if match:
        return ToolCall("manage_memory", {"action": "disable", "what": (match.group("what") or "that").strip()})
    if _MEM_PRIVATE.match(cleaned):
        return ToolCall("manage_memory", {"action": "private", "what": "that"})
    if _MEM_HISTORY.match(cleaned):
        return ToolCall("manage_memory", {"action": "history", "what": "that"})
    return None


def match_goal_or_mission(cleaned: str) -> ToolCall | None:
    remembered = match_memory(cleaned)
    if remembered is not None:
        return remembered
    match = _GOAL.match(cleaned)
    if match:
        goal = match.group("goal").strip(" .,")
        deadline = _GOAL_DEADLINE.search(goal)
        arguments: dict = {}
        if deadline:
            arguments["deadline"] = deadline.group("d").strip()
            goal = goal[:deadline.start()].strip(" ,")
        if len(goal) >= 3:
            return ToolCall("set_goal", {"title": goal, **arguments})
    if _GOALS_LIST.match(cleaned):
        return ToolCall("list_goals", {})
    if _MISSION_RESUME.match(cleaned):
        return ToolCall("resume_mission", {})
    if _MISSION_SKIP.match(cleaned):
        return ToolCall("resume_mission", {"skip_failed": True})
    if _MISSION_CANCEL.match(cleaned):
        return ToolCall("cancel_mission", {})
    if _MISSION_ROLLBACK.match(cleaned):
        return ToolCall("rollback_mission", {})
    if _TASK_CENTER.match(cleaned):
        return ToolCall("task_center", {})
    if _PC_HEALTH.match(cleaned):
        return ToolCall("pc_health", {})
    if _MY_DAY.match(cleaned):
        return ToolCall("jarvis_control", {"action": "my_day"})
    if _DAY_STATUS.match(cleaned):
        return ToolCall("command_center", {"evening": bool(re.search(r"complet|finish|did|do\b|end\s+of\s+day", cleaned, re.I))})
    if _LIST_AGENTS.match(cleaned):
        return ToolCall("list_agents", {})
    if _EXPLAIN.match(cleaned):
        return ToolCall("explain_action", {})
    if _DIAGNOSE.match(cleaned):
        return ToolCall("diagnose_jarvis", {})
    if _REPAIR.match(cleaned):
        return ToolCall("repair_jarvis", {})
    if _MISSION_STATUS.match(cleaned):
        return ToolCall("mission_status", {})
    return None


def match_media_layout_dev(cleaned: str, known_app: Callable[[str], str | None]) -> ToolCall | None:
    planned = match_goal_or_mission(cleaned)
    if planned is not None:
        return planned
    if _UNDO_JARVIS.match(cleaned):
        return ToolCall("undo_last_action", {})
    if _UNDO_LIST.match(cleaned):
        return ToolCall("list_undoable", {})
    if _YT_SKIP_AD.match(cleaned):
        return ToolCall("youtube_control", {"action": "skip_ad"})
    if _YT_LIKE.match(cleaned):
        return ToolCall("youtube_control", {"action": "like"})
    match = _YT_SEEK.match(cleaned)
    if match:
        seconds = _seconds(match.group("n"), match.group("unit"))
        if seconds is not None:
            back = match.group("dir").lower().startswith(("back", "rewind"))
            return ToolCall("youtube_control", {"action": "back" if back else "forward", "seconds": seconds})
    if _YT_RESTART.match(cleaned):
        return ToolCall("youtube_control", {"action": "restart"})
    if _YT_NOW.match(cleaned):
        return ToolCall("youtube_control", {"action": "now_playing"})
    if _YT_SHOW_QUEUE.match(cleaned):
        return ToolCall("youtube_control", {"action": "show_queue"})
    if _YT_CLEAR.match(cleaned):
        return ToolCall("youtube_control", {"action": "clear_queue"})
    if _YT_REPEAT.match(cleaned):
        return ToolCall("youtube_control", {"action": "repeat"})
    match = _YT_QUEUE.match(cleaned)
    if match:
        query = next(q for q in (match.group("q"), match.group("q2"), match.group("q3"), match.group("q4")) if q)
        query = query.strip(" ,.\"'")
        if query and query.lower() not in ("it", "this", "that", "something", "music", "a song"):
            return ToolCall("youtube_control", {"action": "queue", "query": query})
    if _YT_CAPTIONS.match(cleaned):
        return ToolCall("youtube_control", {"action": "captions"})
    if _YT_FULL.match(cleaned):
        return ToolCall("youtube_control", {"action": "fullscreen"})
    if _YT_MUTE.match(cleaned):
        return ToolCall("youtube_control", {"action": "mute_video"})
    match = _SIDE.match(cleaned)
    if match:
        first, second = (match.group("a") or match.group("a2")), (match.group("b") or match.group("b2"))
        a, b = _app_or_this(first, known_app), _app_or_this(second, known_app)
        if a and b:
            return ToolCall("arrange_windows", {"action": "side_by_side", "app": a, "second_app": b})
    match = _NEXT_SCREEN.match(cleaned)
    if match:
        app = _app_or_this(match.group("app"), known_app)
        if app is not False:
            return ToolCall("arrange_windows", {"action": "next_screen", **({"app": app} if app else {})})
    match = _PLACE.match(cleaned)
    if match:
        app = _app_or_this(match.group("app"), known_app)
        if app is not False:
            where = _WHERE[" ".join(match.group("where").lower().split())]
            return ToolCall("arrange_windows", {"action": where, **({"app": app} if app else {})})
    if _MAX_ALL.match(cleaned):
        return ToolCall("arrange_windows", {"action": "maximize_all"})
    match = _LAYOUT_SAVE.match(cleaned)
    if match:
        return ToolCall("arrange_windows", {"action": "save", "layout": (match.group("n") or match.group("n2")).strip()})
    if _LAYOUT_LIST.match(cleaned):
        return ToolCall("arrange_windows", {"action": "list"})
    match = _LAYOUT_DELETE.match(cleaned)
    if match:
        return ToolCall("arrange_windows", {"action": "delete", "layout": match.group("n").strip()})
    match = _LAYOUT_RESTORE.match(cleaned)
    if match and not cleaned.lower().startswith("save") and match.group("n").strip().lower() not in (
            "window", "the", "my", "this", "keyboard", "screen"):
        return ToolCall("arrange_windows", {"action": "restore", "layout": match.group("n").strip()})
    if _RUN_PROJECT.match(cleaned):
        return ToolCall("run_project", {})
    match = _COMMIT.match(cleaned)
    if match and match.group("m").strip(" .\"'"):
        return ToolCall("git_commit", {"message": match.group("m").strip(" .\"'"), "push": bool(match.group("push"))})
    if _EXPLAIN_ERROR.match(cleaned):
        return ToolCall("describe_screen", {"question": "What does the error on the screen mean, and how do I fix it?"})
    if _WHATSAPP_READ.match(cleaned):
        return ToolCall("read_whatsapp", {})
    if _EMAILS.match(cleaned):
        return ToolCall("read_emails", {"unread_only": True})
    return None


def match_shortcut(
    text: str, known_app: Callable[[str], str | None], known_workflow: Callable[[str], str | None] | None = None,
    known_site: Callable[[str], bool] | None = None,
    power_pending: Callable[[], bool] | None = None,
    known_device: Callable[[str], str | None] | None = None,
    known_setting: Callable[[str], bool] | None = None,
    known_alias: Callable[[str], ToolCall | None] | None = None,
    known_computer: Callable[[str], bool] | None = None,
) -> ToolCall | None:
    cleaned = " ".join(text.split()).strip()
    if known_alias is not None:  # your nicknames first: "code", "open my university"
        name = re.sub(r"^(?:please\s+)?(?:open|start|launch|show|go\s+to)\s+", "", bare_keyword(cleaned) or "", flags=re.I)
        aliased = known_alias(name) if name else None
        if aliased is not None:
            return aliased
    voice = match_voice_control(cleaned, known_setting)
    if voice is not None and not (voice.name == "change_setting" and known_device is not None
                                  and known_device(voice.arguments.get("setting", ""))):
        return voice
    session = match_sessions_and_research(cleaned) if len(cleaned) <= 320 else None
    if session is not None:
        return session
    if len(cleaned) > 60:
        return None
    level = level_command(cleaned)
    if level is not None:
        return level
    device = match_devices(cleaned, known_computer)
    if device is not None:
        return device
    evolution = match_evolution(cleaned)
    if evolution is not None:
        return evolution
    special = match_media_layout_dev(cleaned, known_app)
    if special is not None:
        return special
    if _NOTES_START.match(cleaned):
        return ToolCall("meeting_notes", {"action": "start"})
    todo = _TODO_ADD.match(cleaned) if len(cleaned) <= 160 else None
    if todo:
        arguments = {"text": todo.group("text").strip()}
        if todo.group("due"):
            arguments["due"] = todo.group("due").lower().removeprefix("on ").strip()
        return ToolCall("add_todo", arguments)
    if _TODO_LIST.match(cleaned):
        return ToolCall("list_todos", {})
    search = _SEARCH_ON.match(cleaned) or _SEARCH_ON_FIRST.match(cleaned) or _SEARCH_BN.match(cleaned)
    if search:
        engine = (search.group("engine") or search.groupdict().get("engine2") or "").lower()
        engine = "youtube" if engine.startswith(("youtube", "ইউটিউব")) else "google"
        query = search.group("query").strip(" .,!?\"'")
        if query:
            return ToolCall("search_in_browser", {"query": query, "engine": engine})
    close_all = _CLOSE_ALL.match(cleaned) or _CLOSE_ALL_BN.match(cleaned)
    if close_all:
        keep = (close_all.groupdict().get("keep") or "").strip(" ,.")
        return ToolCall("close_all_apps", {"keep": keep} if keep else {})
    focus = focus_command(cleaned)
    if focus is not None:
        return focus
    recall = _RECALL.match(cleaned)
    if recall:
        return ToolCall("recall_activity", {"period": (recall.group("period") or "today").strip()})
    recall = _RECALL_BN.match(cleaned)
    if recall:
        when = " ".join((recall.group("when") or "আজ").split())
        return ToolCall("recall_activity", {"period": _BN_PERIODS.get(when, "today")})
    browser = _BROWSER_PROFILE.match(cleaned)
    if browser and not _NOT_A_PROFILE.match(browser.group("profile")):
        return ToolCall("open_application", {"name": browser.group("browser").lower(),
                                             "profile": browser.group("profile").strip()})
    if known_device is not None:
        home = _HOME.match(cleaned) or _HOME_AFTER.match(cleaned)
        if home:
            device = known_device(home.group("device"))
            if device:
                return ToolCall("home_control", {"device": device, "action": home.group("state").lower()})
    if _EXPLICIT_CANCEL.match(cleaned) or (power_pending is not None and power_pending()
                                           and _CANCEL_POWER.match(cleaned)):
        return ToolCall("power_control", {"action": "cancel_shutdown"})
    match = _POWER.match(cleaned)
    if match:
        verb = match.group("verb").lower()
        if verb.startswith(("power", "turn")) and not match.group("device"):
            return None  # "turn off the lights" / a bare "turn off" is not about the PC
        action = "restart" if verb in ("restart", "reboot") else "shutdown"
        arguments: dict = {"action": action, "delay_seconds": 10}
        if match.group("save"):
            arguments["save_work"] = True
        return ToolCall("power_control", arguments)
    match = _RESTART_APP.match(cleaned)
    if match and not _NOT_RESTARTABLE.match(match.group("name").strip(" ,.")):
        return ToolCall("restart_application", {"name": match.group("name").strip(" ,.")})
    if _LOOK_SCREEN.match(cleaned):
        return ToolCall("describe_screen", {"target": "screen"})
    if _UPDATE.match(cleaned):
        return ToolCall("windows_update", {"action": "install"})
    if _UPDATE_LIST.match(cleaned):
        return ToolCall("windows_update", {"action": "list"})
    if _LOCK.match(cleaned):
        return ToolCall("power_control", {"action": "lock"})
    if _TIME.match(cleaned):
        return ToolCall("get_datetime", {})
    if _BATTERY.match(cleaned):
        return ToolCall("system_status", {"aspect": "battery"})
    info = _info_shortcut(cleaned)
    if info is not None:
        return info
    if _MINIMIZE_ALL.match(cleaned):
        return ToolCall("window_control", {"action": "minimize_all"})
    match = _WINDOW.match(cleaned)
    if match:
        name = match.group("name").strip(" ,.")
        verb = match.group("verb").lower()
        action = ("minimize" if verb.startswith("minimi") else "maximize" if verb.startswith("maximi")
                  else "restore" if verb == "restore" else "focus")
        if name and name.lower() not in _NOT_APPS and len(name.split()) <= 4 and known_app(name) is not None:
            return ToolCall("window_control", {"action": action, "name": name})
        if action != "focus":
            return None
    match = _IN_BROWSER.match(cleaned)
    if match and match.group("browser").lower() not in ("browser", "default browser") \
            and len(match.group("name").split()) <= 4 \
            and not re.search(r"\b(?:file|folder|document|pdf|it|that|this)\b", match.group("name"), re.I):
        # "open Netmirror in Google Chrome": the user named the browser, so it's a site (unknown ones are searched)
        return ToolCall("open_website", {"site": match.group("name").strip(" ,."),
                                         "browser": match.group("browser").lower()})
    if known_site is not None:
        match = _IN_BROWSER.match(cleaned)
        if match and known_site(match.group("name").strip(" ,.")):
            browser = match.group("browser").lower()
            arguments = {"site": match.group("name").strip(" ,.")}
            if browser not in ("browser", "default browser"):
                arguments["browser"] = browser
            return ToolCall("open_website", arguments)
        match = _OPEN_SITE.match(cleaned)
        if match and known_site(match.group("name").strip(" ,.")):
            # a known site: the installed app if there is one (WhatsApp, Instagram...), else the browser
            return ToolCall("open_website", {"site": match.group("name").strip(" ,.")})
    if known_workflow is not None:
        match = _WORKFLOW.match(cleaned)
        if match:
            workflow = known_workflow(match.group("name").strip(" ,."))
            if workflow is not None:
                return ToolCall("run_workflow", {"name": workflow})
    from app.tools.windows_settings import find_page

    settings = _SETTINGS_PAGE.match(cleaned)
    if settings and find_page(settings.group("name")) is not None:  # "bluetooth settings", "open display settings"
        return ToolCall("open_settings_page", {"page": settings.group("name").strip(" ,.")})
    for pattern, tool in _PATTERNS:
        match = pattern.match(cleaned)
        if not match:
            continue
        name = match.group("name").strip(" ,.")
        if not name or name.lower() in _NOT_APPS or len(name.split()) > 4:
            return None
        if any(word in name.lower() for word in ("folder", "file", ".", "\\", "/", "website", "site", "url")):
            return None  # files and websites have their own tools
        if tool == "open_application":
            page = find_page(name, fuzzy=False)
            if page is not None and page[1] != "Settings":  # "open device manager", "open bluetooth"
                return ToolCall("open_settings_page", {"page": name})
        if known_app(name) is None:
            if tool == "open_application" and find_page(name) is not None:
                return ToolCall("open_settings_page", {"page": name})
            break  # not a known app: maybe a keyword, else the last resort below
        return ToolCall(tool, {"name": name})
    return match_keyword(cleaned, known_app, known_site, known_workflow) or match_open_any(cleaned)


# "open Chrome and Spotify", "close Notepad, then lock my PC"
_SPLIT = re.compile(r"\s*,\s*(?:and\s+)?(?:then\s+)?|\s+and\s+then\s+|\s+and\s+also\s+|\s+and\s+|\s+then\s+", re.I)
_VERB = re.compile(r"^(?:please\s+)?(?P<verb>open|launch|start|run|close|quit|exit|minimi[sz]e|maximi[sz]e|switch\s+to)\s+", re.I)
MAX_STEPS = 4


def split_commands(text: str) -> list[str]:
    """Parts of a request that chains simple commands. A part without a verb borrows the previous one:
    "open Chrome and Spotify" -> ["open Chrome", "open Spotify"]. Single commands give []."""
    cleaned = " ".join(text.split()).strip().rstrip(".!?")
    if len(cleaned) > 160:
        return []
    parts = [p.strip() for p in _SPLIT.split(cleaned) if p and p.strip()]
    if not 2 <= len(parts) <= MAX_STEPS:
        return []
    out: list[str] = []
    verb = ""
    for part in parts:
        match = _VERB.match(part)
        if match:
            verb = match.group("verb")
            out.append(part)
        elif verb and len(part.split()) <= 4 and not re.match(r"^(?:lock|restart|reboot|shut|save|turn|power|what|how|tell)\b", part, re.I):
            out.append(f"{verb} {part}")
        else:
            out.append(part)
    return out
