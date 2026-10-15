"""Bangla + English: which language the user used, Bangla commands, and Bangla replies.

* ``detect_language``: Bengali script -> "bn"; romanized Bangla ("chrome kholo") -> "bn";
  otherwise "en". Used to answer in the user's language without any setting.
* ``to_english_command``: common Bangla / Banglish commands -> the English command the direct
  command matcher understands ("ক্রোম খোলো" -> "open chrome", "কয়টা বাজে" -> "what time is it").
  The command still goes through the same checks; only the wording is translated.
* ``localize``: the fixed replies of direct commands ("Opened Google Chrome.") in Bangla.
"""

from __future__ import annotations

import re

BENGALI = re.compile(r"[ঀ-৿]")
_BANGLISH_WORDS = re.compile(
    r"\b(kholo|khulo|khol|bondho|bondo|koro|kor|korо|dao|daw|baje|kemon|ache|acho|achen|koto|kotha|ami|tumi|"
    r"apni|amar|tomar|ekhon|aj|ajke|kal|gaan|gan|chalao|thamao|baro|barao|komao|shuno|bolo|keno|ki|kivabe|"
    r"dekhao|khujo|lagbe|pari|parbe|na|hobe|valo|bhalo|dhonnobad|shobai)\b", re.I)

# app / site names people say in Bangla script -> the English name
APP_NAMES_BN = {
    "গুগল ক্রোম": "google chrome", "ক্রোম": "chrome", "ইউটিউব": "youtube", "ফেসবুক": "facebook",
    "হোয়াটসঅ্যাপ": "whatsapp", "হোয়াটসআপ": "whatsapp", "হোয়াটস অ্যাপ": "whatsapp", "স্পটিফাই": "spotify",
    "নোটপ্যাড": "notepad", "নোট প্যাড": "notepad", "ক্যালকুলেটর": "calculator", "ভিএস কোড": "vs code",
    "ভিস্যুয়াল স্টুডিও কোড": "visual studio code", "ভিজ্যুয়াল স্টুডিও কোড": "visual studio code",
    "ফাইল এক্সপ্লোরার": "file explorer", "টেলিগ্রাম": "telegram", "ডিসকর্ড": "discord", "এজ": "edge",
    "মাইক্রোসফট এজ": "microsoft edge", "সেটিংস": "settings", "ইনস্টাগ্রাম": "instagram", "জিমেইল": "gmail",
    "ফোন লিংক": "phone link", "ফোন লিঙ্ক": "phone link", "টাস্ক ম্যানেজার": "task manager",
    "মেসেঞ্জার": "messenger", "গুগল": "google", "গিটহাব": "github", "ওয়ার্ড": "word", "এক্সেল": "excel",
    "পাওয়ারপয়েন্ট": "powerpoint", "টার্মিনাল": "terminal", "পেইন্ট": "paint", "ক্যামেরা": "camera",
    "ফটোস": "photos", "জুম": "zoom", "টিমস": "teams", "আউটলুক": "outlook", "নেটফ্লিক্স": "netflix",
    "চ্যাটজিপিটি": "chatgpt", "ডাউনলোডস": "downloads", "ডেস্কটপ": "desktop",
}
_DEVICE_BN = r"(?:পিসি|কম্পিউটার|ল্যাপটপ|সিস্টেম|pc|computer|laptop|system)"
_POLITE_BN = r"(?:\s*(?:প্লিজ|দয়া\s*করে|please|to|তো|একটু|ekটু|ektu|na|না)\s*)*"
_END = r"[\s।.!?]*$"
_AND = re.compile(r"\s+(?:আর|এবং|ও|and|ar|abong)\s+|\s*,\s*", re.I)

_OPEN = r"(?:খোলো|খুলো|খোল|খুলে\s*দাও|খুলুন|চালু\s*করো|চালু\s*কর|ওপেন\s*করো|ওপেন\s*কর|kholo|khulo|khol|khule\s*dao|open\s*koro|chalu\s*koro)"
_CLOSE = r"(?:বন্ধ\s*করো|বন্ধ\s*কর|বন্ধ\s*করে\s*দাও|বন্ধ\s*করুন|ক্লোজ\s*করো|ক্লোজ\s*কর|bondho\s*koro|bondo\s*koro|bondho\s*kore\s*dao|close\s*koro)"
_MIN = r"(?:মিনিমাইজ\s*করো|ছোট\s*করো|minimize\s*koro)"

_FIXED: tuple[tuple[re.Pattern[str], str], ...] = tuple((re.compile(p, re.I), en) for p, en in (
    (rf"^{_POLITE_BN}(?:এখন\s*)?(?:কয়টা|কটা)\s*বাজে{_POLITE_BN}{_END}|^(?:ekhon\s*)?(?:koyta|kota)\s*baje{_END}|^সময়\s*কত{_END}",
     "what time is it"),
    (rf"^(?:আজ|আজকে)\s*(?:কত|কতো)\s*তারিখ{_END}|^আজ\s*কী\s*বার{_END}|^aj(?:ke)?\s*koto\s*tarikh{_END}", "what's the date"),
    (rf"^(?:ব্যাটারি|চার্জ)\s*(?:কত|কতো|কেমন)(?:\s*আছে)?{_END}|^(?:battery|charge)\s*koto(?:\s*ache)?{_END}",
     "how much battery do i have"),
    (rf"^(?:আজকের\s*)?আবহাওয়া\s*(?:কেমন|কী)?(?:\s*আজ)?{_END}|^(?:ajker\s*)?(?:abohawa|weather)\s*kemon{_END}",
     "what's the weather"),
    (rf"^(?:কাল|আগামীকাল)(?:কের)?\s*আবহাওয়া\s*(?:কেমন)?{_END}|^(?:কাল|আগামীকাল)\s*কি\s*বৃষ্টি\s*হবে{_END}",
     "weather tomorrow"),
    (rf"^{_POLITE_BN}(?:{_DEVICE_BN}\s*)?লক\s*(?:করো|কর|করে\s*দাও|করুন){_POLITE_BN}{_END}|^(?:pc\s*)?lock\s*koro{_END}",
     "lock my pc"),
    (rf"^(?:{_DEVICE_BN}\s*)?(?:রিস্টার্ট|restart)\s*(?:করো|কর|করে\s*দাও|dao|koro){_END}", "restart my pc"),
    (rf"^(?:{_DEVICE_BN}\s*)(?:শাটডাউন\s*করো|শাট\s*ডাউন\s*করো|বন্ধ\s*করো|বন্ধ\s*করে\s*দাও|bondho\s*koro|shutdown\s*koro){_END}"
     rf"|^শাটডাউন\s*(?:করো|কর|করে\s*দাও){_END}", "shut down my pc"),
    (rf"^(?:ভলিউম|সাউন্ড|আওয়াজ|volume|sound)\s*(?:বাড়াও|বাড়িয়ে\s*দাও|বাড়াও|barao|bariye\s*dao){_END}", "volume up"),
    (rf"^(?:ভলিউম|সাউন্ড|আওয়াজ|volume|sound)\s*(?:কমাও|কমিয়ে\s*দাও|komao|komiye\s*dao){_END}", "volume down"),
    (rf"^(?:সাউন্ড\s*)?মিউট\s*(?:করো|কর|করে\s*দাও){_END}|^mute\s*koro{_END}", "mute"),
    (rf"^(?:গান|মিউজিক|ভিডিও|gaan|gan|music)\s*(?:থামাও|বন্ধ\s*করো|পজ\s*করো|thamao|pause\s*koro){_END}", "pause"),
    (rf"^(?:গান|মিউজিক|gaan|gan|music)\s*(?:চালাও|চালু\s*করো|বাজাও|chalao|chalu\s*koro){_END}", "play some music"),
    (rf"^(?:পরের|পরবর্তী|next)\s*(?:গান|ট্র্যাক|ভিডিও|gaan|gan)?\s*(?:চালাও|দাও|chalao|dao)?{_END}", "next song"),
    (rf"^(?:আগের|previous)\s*(?:গান|ট্র্যাক|gaan|gan)\s*(?:চালাও|দাও|chalao|dao)?{_END}", "previous track"),
    (rf"^(?:শুভ\s*সকাল|সুপ্রভাত|গুড\s*মর্নিং)(?:\s*জার্ভিস)?{_END}|^আমার\s*দিনের\s*খবর\s*দাও{_END}", "good morning"),
    (rf"^(?:সব\s*)?(?:উইন্ডো|window)\s*(?:মিনিমাইজ\s*করো|ছোট\s*করো)|^ডেস্কটপ\s*দেখাও{_END}", "show the desktop"),
    (rf"^(?:ডেস্কটপ(?:ের)?\s*(?:সব\s*)?(?:ফাইল(?:গুলো)?\s*)?(?:গুছিয়ে\s*(?:দাও|রাখো)|গোছাও|অর্গানাইজ\s*করো)){_END}"
     rf"|^(?:desktop\s*)?(?:organize|gochao|guchiye\s*dao){_END}", "organize my desktop"),
    (rf"^(?:রিসাইকেল\s*বিন|recycle\s*bin)\s*(?:খালি\s*করো|পরিষ্কার\s*করো|ক্লিয়ার\s*করো|khali\s*koro|clear\s*koro){_END}",
     "empty the recycle bin"),
    (rf"^(?:আজকের\s*)?(?:খবর|সংবাদ|শিরোনাম|হেডলাইন)\s*(?:কী|দাও|বলো|শোনাও)?{_END}|^(?:ajker\s*)?khobor\s*(?:dao|bolo)?{_END}",
     "what's the news"),
))


def detect_language(text: str) -> str:
    """ "bn" for Bangla (Bengali script, or romanized Bangla), else "en"."""
    if BENGALI.search(text or ""):
        return "bn"
    words = re.findall(r"[a-z]+", (text or "").lower())
    hits = sum(1 for w in words if _BANGLISH_WORDS.fullmatch(w))
    return "bn" if words and hits >= max(1, len(words) // 3) and hits >= 1 and len(words) <= 12 and _looks_banglish(words) else "en"


def _looks_banglish(words: list[str]) -> bool:
    strong = {"kholo", "khulo", "bondho", "bondo", "koro", "dao", "baje", "kemon", "koto", "chalao", "thamao",
              "barao", "komao", "ache", "acho", "achen", "ekhon", "ajke", "dhonnobad", "bolo", "kivabe", "lagbe"}
    return any(w in strong for w in words)


def _app_name(name: str) -> str:
    name = " ".join(name.split()).strip(" ,।")
    for bn, en in sorted(APP_NAMES_BN.items(), key=lambda kv: -len(kv[0])):
        name = name.replace(bn, en)
    name = re.sub(r"(?:টা|টি|গুলো|কে|এর|ের)$", "", name).strip()
    return name


def _one(text: str) -> str | None:
    cleaned = re.sub(r"^(?:হে\s*)?(?:জার্ভিস|জারভিস|জার্বিস)[\s,।]*", "", " ".join(text.split()))
    for pattern, english in _FIXED:
        if pattern.match(cleaned):
            return english
    for verb_re, verb in ((_OPEN, "open"), (_CLOSE, "close"), (_MIN, "minimize")):
        match = re.match(rf"^{_POLITE_BN}(?P<name>.+?)\s*(?:অ্যাপ(?:টা)?\s*)?{verb_re}{_POLITE_BN}{_END}", cleaned, re.I)
        if match:
            name = _app_name(match.group("name"))
            if name and not BENGALI.search(name) and len(name.split()) <= 4:
                return f"{verb} {name}"
            return None
    return None


# Speech recognition often writes a spoken Bangla verb as an English word: "Google Chrome খোলো" comes out
# as "Google Chrome Club". Only used for a short name followed by one of these, and the name must still
# be an app the assistant knows (the direct-command matcher checks that).
_MISHEARD_OPEN = re.compile(r"^(?P<name>[A-Za-z][\w .'-]{1,40}?)\s+(?:club|clove|clo|colo|kolo|khulo|kholo|khol|holo|"
                            r"hollow|cola|kulo|koolo|call\s+o|close\s+o)[.!?]*$", re.I)
_MISHEARD_CLOSE = re.compile(r"^(?P<name>[A-Za-z][\w .'-]{1,40}?)\s+(?:bondho|bondo|bandho|bando|bond\s+o|bundle)"
                             r"(?:\s+(?:koro|kor|core|cora|corrode|kodo))?[.!?]*$", re.I)


def misheard_bangla_command(text: str) -> str | None:
    """ "Google Chrome Club" -> "open Google Chrome" (a Bangla "খোলো" heard as English)."""
    text = " ".join((text or "").split())
    for pattern, verb in ((_MISHEARD_OPEN, "open"), (_MISHEARD_CLOSE, "close")):
        match = pattern.match(text)
        if match and len(match.group("name").split()) <= 3:
            return f"{verb} {match.group('name').strip()}"
    return None


def to_english_command(text: str) -> str | None:
    """A Bangla/Banglish command as the English command, or None if it isn't a simple command."""
    text = " ".join((text or "").split())
    if not text or len(text) > 120:
        return None
    single = _one(text)
    if single is not None:
        return single
    parts = [p for p in _AND.split(text) if p.strip()]
    if len(parts) < 2:
        return None
    # "ক্রোম আর স্পটিফাই খোলো": the verb comes once, at the end
    last = _one(parts[-1])
    if last is None:
        return None
    verb = last.split()[0]
    english = []
    for part in parts[:-1]:
        converted = _one(part)
        if converted is None:
            name = _app_name(part)
            if not name or BENGALI.search(name):
                return None
            converted = f"{verb} {name}"
        english.append(converted)
    english.append(last)
    return " and ".join(english)


# ------------------------------------------------------------------ Bangla replies

_REPLIES: tuple[tuple[re.Pattern[str], str], ...] = tuple((re.compile(p), bn) for p, bn in (
    (r"^Opened (.+?) in (.+?)\.$", r"\1, \2-এ খুলে দিয়েছি।"),
    (r"^Opened (.+?)\.$", r"\1 খুলে দিয়েছি।"),
    (r"^Closed (.+?)\.$", r"\1 বন্ধ করে দিয়েছি।"),
    (r"^(.+?) doesn't seem to be open\.$", r"\1 এখন খোলা নেই।"),
    (r"^Minimized all windows\.$", "সব উইন্ডো মিনিমাইজ করে দিয়েছি।"),
    (r"^Minimized (.+?)\.$", r"\1 মিনিমাইজ করেছি।"),
    (r"^Maximized (.+?)\.$", r"\1 বড় করে দিয়েছি।"),
    (r"^Switched to (.+?)\.$", r"\1-এ চলে গেছি।"),
    (r"^It is (\d{1,2}:\d{2}) on (\w+), (.+)\.$", r"এখন সময় \1।"),
    (r"^The battery is at (\d+)%, charging\.$", r"ব্যাটারি \1%, চার্জ হচ্ছে।"),
    (r"^The battery is at (\d+)%, on battery power\.$", r"ব্যাটারি \1% আছে, চার্জার লাগানো নেই।"),
    (r"^Volume set to (\d+)%\.$", r"ভলিউম \1% করে দিয়েছি।"),
    (r"^Muted\.$", "সাউন্ড মিউট করে দিয়েছি।"),
    (r"^Unmuted\. Volume is (\d+)%\.$", r"সাউন্ড চালু করেছি, ভলিউম \1%।"),
    (r"^Toggled play/pause\.$", "ঠিক আছে, গান থামিয়ে/চালিয়ে দিয়েছি।"),
    (r"^Playing (.+?) on YouTube in (.+?)\.$", r"YouTube-এ \1 চালিয়ে দিয়েছি, উপভোগ করুন!"),
    (r"^Next track\.$", "পরের গান চালাচ্ছি।"),
    (r"^Previous track\.$", "আগের গান চালাচ্ছি।"),
    (r"^Stopped playback\.$", "গান বন্ধ করে দিয়েছি।"),
    (r"^Locked the computer\.$", "পিসি লক করে দিয়েছি।"),
    (r"^Restarting in (\d+) seconds\.$", r"\1 সেকেন্ডের মধ্যে পিসি রিস্টার্ট হবে।"),
    (r"^Shutting down in (\d+) seconds\.$", r"\1 সেকেন্ডের মধ্যে পিসি বন্ধ হয়ে যাবে।"),
    (r"^Saved (\d+) window\(s\) first\.$", r"আগে \1টি উইন্ডোর কাজ সেভ করে নিয়েছি।"),
    (r"^Nothing needed saving\.$", "সেভ করার মতো কিছু ছিল না।"),
    (r"^Say 'cancel' or press Esc to stop it\.$", "থামাতে চাইলে 'cancel' বলুন বা Esc চাপুন।"),
    (r"^Cancelled\. Nothing will restart or shut down\.$", "বাতিল করেছি, পিসি রিস্টার্ট বা বন্ধ হবে না।"),
    (r"^Organized (\d+) files on your Desktop into the 'Organized Desktop' folder \((.+)\)\.$",
     r"ডেস্কটপের \1টি ফাইল 'Organized Desktop' ফোল্ডারে গুছিয়ে রেখেছি।"),
    (r"^Say 'undo organize' to put them back\.$", "আগের মতো ফিরিয়ে আনতে চাইলে 'undo organize' বলবেন।"),
    (r"^Your Desktop is already tidy.*$", "ডেস্কটপ আগে থেকেই গোছানো আছে।"),
    (r"^Put (\d+) files back on your Desktop\.$", r"\1টি ফাইল আবার ডেস্কটপে ফিরিয়ে দিয়েছি।"),
    (r"^Emptied the Recycle Bin: (\d+) item\(s\), (.+) freed\.$", r"রিসাইকেল বিন খালি করেছি, \1টি আইটেম মুছে \2 জায়গা খালি হলো।"),
    (r"^The Recycle Bin is already empty\.$", "রিসাইকেল বিন আগে থেকেই খালি।"),
    (r"^In (.+?) it's (-?\d+)°([CF]) with (.+?)(?:, feels like (-?\d+)°[CF])?\.$", r"\1-এ এখন তাপমাত্রা \2°\3, আকাশ \4।"),
))


def localize(message: str, language: str) -> str:
    """A direct command's English reply in ``language`` (only Bangla is translated)."""
    if language != "bn" or not message:
        return message
    parts = re.split(r"(?<=\.)\s+", message.strip())
    out = []
    for part in parts:
        for pattern, bn in _REPLIES:
            if pattern.match(part):
                out.append(pattern.sub(bn, part))
                break
        else:
            out.append(part)
    return " ".join(out)


# ------------------------------------------------------------------ wake / presence in Bangla

BANGLA_NAMES = ("জার্ভিস", "জারভিস", "জার্বিস", "জাভিস", "জার্ভিশ", "জারবিস", "যার্ভিস", "জার্ভিজ")
_BN_PRESENCE = re.compile(r"^(?:(?:হে|হ্যালো|এই)\s*)?(?:জার্ভিস|জারভিস|জার্বিস|জাভিস|জার্ভিশ|জারবিস|যার্ভিস|জার্ভিজ)?[\s,।]*"
                          r"(?:তুমি\s*|আপনি\s*)?(?:আছো|আছ|আছেন|শুনছো|শুনছ|শুনতে\s*পাচ্ছো|শুনছেন|কোথায়)?[\s?।!]*$")
_BN_DISMISS = re.compile(r"^(?:ঠিক\s*আছে[\s,]*)?(?:ধন্যবাদ[\s,]*)?(?:বিদায়|আল্লাহ\s*হাফেজ|আর\s*কিছু\s*(?:লাগবে\s*)?না|এখন\s*বিশ্রাম\s*নাও|"
                         r"ঘুমাও|থাক|শেষ|আপাতত\s*এটুকুই)[\s।!.]*$")


def strip_bangla_wake(text: str) -> tuple[str, bool]:
    """ "জার্ভিস, ক্রোম খোলো" -> ("ক্রোম খোলো", True)."""
    match = re.match(r"^\s*(?:(?:হে|হেই|হ্যালো|এই|ও)\s*)?(?:জার্ভিস|জারভিস|জার্বিস|জাভিস|জার্ভিশ|জারবিস|যার্ভিস|জার্ভিজ)[\s,।!?]*", text or "")
    if match:
        return text[match.end():].strip(), True
    return (text or "").strip(), False


def is_bangla_presence(text: str) -> bool:
    return bool(BENGALI.search(text or "")) and bool(_BN_PRESENCE.match(text.strip()))


def is_bangla_dismissal(text: str) -> bool:
    return bool(_BN_DISMISS.match((text or "").strip()))


def bangla_greeting(address: str) -> str:
    return "জি স্যার, বলুন, কীভাবে সাহায্য করতে পারি?" if address.strip().lower() == "sir" else \
        "জি, বলুন, কীভাবে সাহায্য করতে পারি?"


def bangla_dismissal(address: str) -> str:
    return "ঠিক আছে স্যার। দরকার হলে শুধু জার্ভিস বলবেন।" if address.strip().lower() == "sir" else \
        "ঠিক আছে। দরকার হলে শুধু জার্ভিস বলবেন।"


# ------------------------------------------------------------------ talking over the assistant

_STOP = re.compile(r"^(?:(?:ok(?:ay)?|hey|jarvis)[\s,]*)*(?:stop|stop it|stop talking|be quiet|quiet|shut up|enough|"
                   r"wait|hold on|cancel|pause|never mind|that's enough|থামো|থামুন|থাম|চুপ|চুপ করো|থাক|বাস|আর না|"
                   r"থেমে যাও|দাঁড়াও|এক মিনিট)[\s.!।]*$", re.I)


def is_stop_request(text: str) -> bool:
    return bool(_STOP.match((text or "").strip()))


def _norm(text: str) -> list[str]:
    return re.findall(r"[\w\u0980-\u09FF]+", (text or "").lower())


def is_echo(heard: str, speaking: str) -> bool:
    """True if what the microphone heard is (part of) what the assistant is saying right now."""
    heard_words, spoken = _norm(heard), set(_norm(speaking))
    if not heard_words or not spoken:
        return False
    overlap = sum(1 for w in heard_words if w in spoken) / len(heard_words)
    return overlap >= 0.6


_YES = re.compile(r"^(?:(?:yes|yeah|yep|yup|sure|ok|okay|go ahead|do it|confirm|confirmed|please do|of course|"
                  r"absolutely|affirmative|right|correct|হ্যাঁ|হা|হাঁ|জি|জ্বি|ঠিক আছে|করো|করুন|হ্যাঁ করো|অবশ্যই|আচ্ছা|ha|haa|hya)"
                  r"[\s,.!।]*)+(?:please|sir|স্যার)?[\s.!।]*$", re.I)
_NO = re.compile(r"^(?:(?:no|nope|nah|don't|do not|cancel|stop|never mind|negative|not now|wait|না|নাহ|না থাক|থাক|"
                 r"বাতিল|করো না|করবেন না|দরকার নেই|na|naa)[\s,.!।]*)+(?:please|sir|স্যার)?[\s.!।]*$", re.I)


def classify_answer(text: str) -> bool | None:
    """Spoken answer to a yes/no question: True, False, or None (not a clear answer)."""
    cleaned = " ".join((text or "").lower().split()).strip(" .!?।,")
    if not cleaned:
        return None
    if _NO.match(cleaned):
        return False
    if _YES.match(cleaned):
        return True
    return None
