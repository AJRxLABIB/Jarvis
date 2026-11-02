"""A labelled mixed-language command set (English, Bangla script, romanised Bangla) and the metrics it yields.

Each case says what a person means: the language, and either the English command it should become (``expect``) or ``None`` when it must
NOT be rewritten (chat, unsafe or too vague - the text then goes on unchanged, to the normal path that asks or refuses). Reported per
language: language-detection accuracy, command accuracy (exact), UNSAFE REWRITES (a case that must not be rewritten but was - the
number that has to stay 0), and missed commands. Cases tagged ``risky`` (lock / restart / shut down / close / empty the bin) are
tracked separately because a wrong rewrite there costs the most.

What it measures: the deterministic Bangla/Banglish -> English command layer (``app/agent/language.py``). It does NOT measure speech
recognition of Bangla (that needs real audio: run.py --benchmark) nor what the model does with text that is passed through, and the set
is small and written by the developer: it catches regressions and known gaps, it is not a claim of general Bangla understanding.
Run: ``python -m app.agent.multilingual_eval``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agent.language import detect_language, to_english_command


@dataclass(frozen=True)
class Case:
    text: str
    language: str                  # "en" | "bn" (Bangla script or romanised)
    expect: str | None             # the English command, or None = must not be rewritten
    tags: tuple[str, ...] = ()     # script | roman | risky | unsafe | vague | chat | unicode | multi


C = Case
CASES: tuple[Case, ...] = (
    # Bangla script, simple commands
    C("ক্রোম খোলো", "bn", "open chrome", ("script",)), C("ইউটিউব খুলে দাও", "bn", "open youtube", ("script",)),
    C("নোটপ্যাড বন্ধ করো", "bn", "close notepad", ("script", "risky")), C("ওয়ার্ড খোলো", "bn", "open word", ("script",)),
    C("গুগল ক্রোম বন্ধ করো", "bn", "close google chrome", ("script", "risky")),
    C("জার্ভিস, ক্রোম খোলো", "bn", "open chrome", ("script",)),
    C("ক্রোম অ্যাপ খোলো", "bn", "open chrome", ("script",)), C("হোয়াটসঅ্যাপ খোলো", "bn", "open whatsapp", ("script", "unicode")),
    C("হোয়াটস অ্যাপ খোলো", "bn", "open whatsapp", ("script", "unicode")),
    C("ক্রোম আর স্পটিফাই খোলো", "bn", "open chrome and open spotify", ("script", "multi")),
    C("ফেসবুক খোলো আর হোয়াটসঅ্যাপ খোলো", "bn", "open facebook and open whatsapp", ("script", "multi")),
    C("এখন কয়টা বাজে", "bn", "what time is it", ("script",)), C("ভলিউম বাড়াও", "bn", "volume up", ("script",)),
    C("ভলিউম কমাও", "bn", "volume down", ("script",)), C("মিউট করো", "bn", "mute", ("script",)),
    C("আজকের আবহাওয়া কেমন", "bn", "what's the weather", ("script",)),
    C("ব্যাটারি কত আছে", "bn", "how much battery do i have", ("script",)),
    C("পিসি লক করো", "bn", "lock my pc", ("script", "risky")), C("ল্যাপটপ লক করো", "bn", "lock my pc", ("script", "risky")),
    C("পিসি রিস্টার্ট করো", "bn", "restart my pc", ("script", "risky")),
    C("পিসি বন্ধ করো", "bn", "shut down my pc", ("script", "risky")), C("কম্পিউটার বন্ধ করো", "bn", "shut down my pc", ("script", "risky")),
    C("রিসাইকেল বিন খালি করো", "bn", "empty the recycle bin", ("script", "risky")),
    # romanised Bangla
    C("chrome kholo", "bn", "open chrome", ("roman",)), C("youtube khulo", "bn", "open youtube", ("roman",)),
    C("spotify bondho koro", "bn", "close spotify", ("roman", "risky")), C("volume barao", "bn", "volume up", ("roman",)),
    C("ekhon koyta baje", "bn", "what time is it", ("roman",)), C("aj koto tarikh", "bn", "what's the date", ("roman",)),
    C("pc lock koro", "bn", "lock my pc", ("roman", "risky")), C("gaan chalao", "bn", "play some music", ("roman",)),
    # Bangla that must NOT be rewritten: chat, destructive or too vague (text passes on unchanged to the normal, confirming path)
    C("ফাইল ডিলিট করো", "bn", None, ("script", "unsafe")), C("সব মুছে ফেলো", "bn", None, ("script", "unsafe")),
    C("আমার ছবিগুলো পাঠাও", "bn", None, ("script", "unsafe")), C("আমাকে একটা গল্প বলো", "bn", None, ("script", "chat")),
    C("ক্রোম", "bn", None, ("script", "vague")), C("খোলো", "bn", None, ("script", "vague")), C("বন্ধ করো", "bn", None, ("script", "vague", "unsafe")),
    C("kholo chrome", "bn", None, ("roman", "vague")), C("bondho koro", "bn", None, ("roman", "vague", "unsafe")),
    # held-out variants (polite words, suffixes, word forms) and NEGATIONS - "do not close Chrome" must never become "close chrome"
    C("প্লিজ ক্রোম খোলো", "bn", "open chrome", ("script", "variant")), C("একটু ইউটিউব খুলে দাও না", "bn", "open youtube", ("script", "variant")),
    C("ক্রোমটা বন্ধ করে দাও", "bn", "close chrome", ("script", "variant", "risky")),
    C("ভলিউম একটু বাড়িয়ে দাও", "bn", "volume up", ("script", "variant")), C("সাউন্ড কমিয়ে দাও", "bn", "volume down", ("script", "variant")),
    C("পিসিটা লক করে দাও", "bn", "lock my pc", ("script", "variant", "risky")),
    C("আমার পিসি বন্ধ করে দাও", "bn", "shut down my pc", ("script", "variant", "risky")),
    C("chrome khule dao", "bn", "open chrome", ("roman", "variant")), C("youtube open koro", "bn", "open youtube", ("roman", "variant")),
    C("ডিসকর্ড আর টেলিগ্রাম বন্ধ করো", "bn", "close discord and close telegram", ("script", "multi", "risky")),
    C("ক্রোম বন্ধ করো না", "bn", None, ("script", "negation", "unsafe")), C("ক্রোম খুলো না", "bn", None, ("script", "negation")),
    C("পিসি বন্ধ করো না", "bn", None, ("script", "negation", "unsafe")), C("chrome bondho koro na", "bn", None, ("roman", "negation", "unsafe")),
    C("shutdown koro na", "bn", None, ("roman", "negation", "unsafe")),
    # English: never rewritten
    C("open chrome", "en", None, ("chat",)), C("what time is it", "en", None, ("chat",)), C("delete everything", "en", None, ("unsafe",)),
    C("shut down my pc", "en", None, ("risky",)), C("hello jarvis", "en", None, ("chat",)), C("kill the process", "en", None, ("unsafe",)),
)


@dataclass
class Stats:
    total: int = 0
    detected: int = 0
    commands: int = 0              # cases that should be rewritten
    commands_right: int = 0
    mustnot: int = 0               # cases that must not be rewritten
    unsafe_rewrites: list[str] = field(default_factory=list)
    risky: int = 0
    risky_right: int = 0
    missed: list[str] = field(default_factory=list)

    def pct(self, a: int, b: int) -> str:
        return f"{100 * a / b:.0f}%" if b else "n/a"


def evaluate(cases: tuple[Case, ...] = CASES) -> dict[str, Stats]:
    out: dict[str, Stats] = {}
    for c in cases:
        s = out.setdefault(c.language, Stats())
        s.total += 1
        s.detected += detect_language(c.text) == c.language
        got = to_english_command(c.text)
        if c.expect is None:
            s.mustnot += 1
            if got is not None:
                s.unsafe_rewrites.append(f"{c.text!r} -> {got!r}")
        else:
            s.commands += 1
            ok = got == c.expect
            s.commands_right += ok
            if not ok:
                s.missed.append(f"{c.text!r}: wanted {c.expect!r}, got {got!r}")
            if "risky" in c.tags:
                s.risky += 1
                s.risky_right += ok
    return out


def report(cases: tuple[Case, ...] = CASES) -> str:
    lines = ["Mixed-language command set (the deterministic command layer only; speech recognition is not measured here):"]
    for lang, s in sorted(evaluate(cases).items()):
        lines.append(f"{'Bangla' if lang == 'bn' else 'English'}: {s.total} cases; language detected {s.pct(s.detected, s.total)}; "
                     f"commands understood {s.commands_right}/{s.commands} ({s.pct(s.commands_right, s.commands)}); "
                     f"risky commands right {s.risky_right}/{s.risky}; unsafe or vague rewrites {len(s.unsafe_rewrites)} of {s.mustnot} (must be 0)")
        lines += [f"  missed: {m}" for m in s.missed] + [f"  WRONGLY REWRITTEN: {m}" for m in s.unsafe_rewrites]
    return "\n".join(lines)


if __name__ == "__main__":
    print(report())
