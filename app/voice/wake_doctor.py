"""Wake doctor: teach Jarvis how YOUR voice's wake phrase comes out of the recognizer.

`python run.py --wake-doctor` asks you to say the wake phrase a few times, shows exactly what speech recognition wrote each
time (on screen only: it is not logged or saved) and whether Jarvis would have woken. When it keeps writing the name
differently ("Travis", "Gervais"), it offers to remember that spelling as YOUR alias for the name - one question per
spelling, nothing added without a yes. Aliases live in data\\wake_aliases.json (delete the file or a line to undo) and only
make the name check accept those spellings; the neural wake model, the personal wake check and the other safeguards stay.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.utils.phonetic import similarity
from app.voice import wake_word

FILE = "wake_aliases.json"
MAX_ALIASES = 12
_TOKEN = re.compile(r"[a-z']+")


def load(data_dir: Path) -> list[str]:
    try:
        data = json.loads((Path(data_dir) / FILE).read_text(encoding="utf-8"))
        return [w for w in (str(x).lower() for x in data.get("aliases", [])) if _TOKEN.fullmatch(w)][:MAX_ALIASES]
    except (OSError, ValueError, AttributeError):
        return []


def save(data_dir: Path, aliases: list[str]) -> None:
    path = Path(data_dir) / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"aliases": sorted({a.lower() for a in aliases if _TOKEN.fullmatch(a.lower())})[:MAX_ALIASES]}, indent=1), encoding="utf-8")


def activate(data_dir: Path) -> list[str]:
    """Load the saved aliases into the matcher (called when the recognizer is created)."""
    aliases = load(data_dir)
    wake_word.set_personal_aliases(aliases)
    return aliases


@dataclass
class Attempt:
    heard: str
    accepted: bool
    name_token: str = ""


@dataclass
class Report:
    attempts: list[Attempt] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    @property
    def hit_rate(self) -> float:
        return sum(a.accepted for a in self.attempts) / len(self.attempts) if self.attempts else 0.0


def name_token(heard: str, wake: str) -> str:
    """The word where the assistant's name should be ("hey travis open chrome" -> "travis")."""
    tokens = _TOKEN.findall(heard.lower())
    greetings = wake_word._GREETING_VARIANTS.get("hey", set())
    wake_words = wake.lower().split()
    if len(wake_words) > 1 and tokens and tokens[0] in greetings and len(tokens) > 1:
        return tokens[1]
    return tokens[0] if tokens else ""


def analyse(heard_list: list[str], wake: str, existing: list[str] = ()) -> Report:
    name = wake.lower().split()[-1] if wake.split() else ""
    report = Report()
    seen: dict[str, int] = {}
    for heard in heard_list:
        _, accepted = wake_word.strip_wake_phrase(heard, wake)
        token = name_token(heard, wake)
        report.attempts.append(Attempt(heard, accepted, token))
        if not accepted and token and token != name and token not in existing and similarity(token, name) >= 0.4:
            seen[token] = seen.get(token, 0) + 1
    report.suggestions = [t for t, _ in sorted(seen.items(), key=lambda kv: -kv[1])]
    return report


def run(settings, data_dir: Path, *, out, ask: Callable[[str], str], listen: Callable[[], str], rounds: int = 6) -> int:
    """The CLI flow. ``listen()`` returns what was recognized for one attempt. Returns 0 when the wake phrase works."""
    wake = settings.wake_word
    out.write(f"Say \"{wake.title()}\" {rounds} times, each time after the prompt, the way you normally do (add a short "
              f"command sometimes, like \"{wake.title()}, open Chrome\").\n\n")
    heard: list[str] = []
    for n in range(1, rounds + 1):
        out.write(f"  {n}/{rounds} - speak now ... ")
        out.flush() if hasattr(out, "flush") else None
        try:
            text = listen()
        except Exception as exc:
            out.write(f"\n{str(exc)[:120]}\n")
            return 1
        heard.append(text)
        _, ok = wake_word.strip_wake_phrase(text, wake)
        out.write(f"heard \"{text or '(nothing)'}\"  -> {'would wake' if ok else 'would NOT wake'}\n")
    existing = load(data_dir)
    report = analyse(heard, wake, existing)
    out.write(f"\nWould have woken {sum(a.accepted for a in report.attempts)} of {len(report.attempts)} times.\n")
    added = []
    for token in report.suggestions:
        if len(existing) + len(added) >= MAX_ALIASES:
            break
        answer = ask(f"It wrote \"{token}\" instead of \"{wake.split()[-1]}\". Treat \"{token}\" as your wake name? [y/N] ")
        if answer.strip().lower() in ("y", "yes"):
            added.append(token)
    if added:
        save(data_dir, existing + added)
        activate(data_dir)
        out.write(f"Saved. From now on {', '.join(added)} also counts as the name (undo: edit data\\{FILE}).\n")
    nothing = sum(1 for a in report.attempts if not a.heard.strip())
    if nothing >= len(report.attempts) / 2:
        out.write("Most attempts produced no words at all: the problem is the microphone, not the wake word. "
                  "Run `python run.py --voice-doctor`.\n")
    elif not added and report.hit_rate < 0.7:
        out.write("It keeps missing but there's no consistent mis-spelling to learn from. Speak closer to the microphone, "
                  "or run `python run.py --voice-doctor` and, if you want, record your own wake sound with the wake-word trainer.\n")
    return 0 if report.hit_rate >= 0.7 or added else 1
