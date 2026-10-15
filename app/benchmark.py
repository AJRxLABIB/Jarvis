"""Your own voice benchmark: record your real commands once, then measure every change.

    python run.py --record-benchmark   say each prompted command (Bangla, English, mixed)
    python run.py --benchmark          replay them: recognition accuracy, command matching, speed
    python run.py --train-voice        learn from them: how you pronounce your commands (see train())

Recordings stay on this PC in data\\benchmark (WAV files + index.json). Each run writes a
report there too (report-<date>.txt), so results can be compared over time.
"""

from __future__ import annotations

import json
import time
import wave
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

import numpy as np

# (what to say, expected tool, a word the tool's arguments must contain or "")
PROMPTS: tuple[tuple[str, str, str], ...] = (
    ("Open Google Chrome", "open_application", "chrome"),
    ("Close Phone Link", "close_application", "phone"),
    ("What time is it?", "get_datetime", ""),
    ("How much battery do I have?", "system_status", "battery"),
    ("Lock my PC", "power_control", "lock"),
    ("Volume up", "volume_control", "up"),
    ("Pause the music", "media_control", ""),
    ("Play Arijit Singh songs", "play_music", "arijit"),
    ("What's the weather like today?", "get_weather", ""),
    ("Minimize Chrome", "window_control", "minimize"),
    ("Open Chrome and Notepad", "open_application", "chrome"),
    ("Organize my desktop", "organize_desktop", ""),
    ("ক্রোম খোলো", "open_application", "chrome"),
    ("এখন কয়টা বাজে?", "get_datetime", ""),
    ("ভলিউম বাড়াও", "volume_control", "up"),
    ("গান থামাও", "media_control", ""),
    ("পিসি লক করো", "power_control", "lock"),
    ("আবহাওয়া কেমন?", "get_weather", ""),
    ("ইউটিউব খুলে দাও", "open_website", "youtube"),
    ("Google Chrome খোলো", "open_application", "chrome"),
    ("আজকের খবর শোনাও", "get_news", ""),
    ("ব্যাটারি কত আছে?", "system_status", "battery"),
)


# How to say the Bangla prompts, for consoles that show Bangla script as empty boxes.
ROMANIZED = {
    "ক্রোম খোলো": "chrome kholo", "এখন কয়টা বাজে?": "ekhon koyta baje?", "ভলিউম বাড়াও": "volume barao",
    "গান থামাও": "gaan thamao", "পিসি লক করো": "PC lock koro", "আবহাওয়া কেমন?": "abohawa kemon?",
    "ইউটিউব খুলে দাও": "YouTube khule dao", "Google Chrome খোলো": "Google Chrome kholo",
    "আজকের খবর শোনাও": "ajker khobor shonao", "ব্যাটারি কত আছে?": "battery koto ache?",
}


def spoken_hint(say: str) -> str:
    """The prompt plus its romanized form (in Bangla, said in Bangla)."""
    roman = ROMANIZED.get(say)
    return f"{say}   ({roman} - say it in Bangla)" if roman else say


@dataclass(frozen=True)
class Item:
    file: str
    say: str
    tool: str
    contains: str


def folder(data_dir: Path) -> Path:
    return Path(data_dir) / "benchmark"


def save_wav(path: Path, audio: np.ndarray, rate: int = 16000) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as src:
        data = np.frombuffer(src.readframes(src.getnframes()), dtype=np.int16)
    return (data.astype(np.float32) / 32768.0).reshape(-1)


MIN_SECONDS, MIN_PEAK_DB = 0.5, -40.0


def audio_ok(audio: np.ndarray) -> bool:
    """A usable recording: long enough and actually containing speech-level sound."""
    if audio.size < 16000 * MIN_SECONDS:
        return False
    peak = float(np.max(np.abs(audio)))
    return peak > 10 ** (MIN_PEAK_DB / 20)


def trim_silence(audio: np.ndarray, rate: int = 16000, before: float = 0.3, after: float = 0.4) -> np.ndarray:
    """Cut a fixed-length recording down to the speech in it (plus a little room either side)."""
    if audio.size == 0:
        return audio
    frame = int(rate * 0.03)
    count = audio.size // frame
    if count < 3:
        return audio
    energy = np.sqrt(np.mean(audio[: count * frame].reshape(count, frame) ** 2, axis=1))
    floor = float(np.percentile(energy, 20))
    threshold = max(floor * 3.0, float(energy.max()) * 0.12, 1e-4)
    loud = np.nonzero(energy > threshold)[0]
    if loud.size == 0:
        return audio
    start = max(0, loud[0] * frame - int(before * rate))
    end = min(audio.size, (loud[-1] + 1) * frame + int(after * rate))
    return audio[start:end]


def level_advice(peak_db: float) -> str:
    """A hint when the microphone is too quiet (short commands then come out as "Thank you.")."""
    if peak_db < -35:
        return ("   Your microphone is very quiet here: move closer, or raise the input level in Windows "
                "(Settings > System > Sound > your microphone > Input volume).")
    if peak_db < -26:
        return "   A bit quiet: speak up a little or move closer to the microphone."
    return ""


def record(data_dir: Path, recorder: Callable[[], np.ndarray], prompt: Callable[[str, int, int], None],
           retry: Callable[[str], None] | None = None, attempts: int = 3, only_bad: bool = True) -> int:
    """Record every prompt (``recorder()`` returns 16 kHz audio of one utterance). A recording that is
    empty or too quiet is asked for again. With ``only_bad``, good recordings from an earlier run are kept."""
    target = folder(data_dir)
    target.mkdir(parents=True, exist_ok=True)
    items, recorded = [], 0
    for number, (say, tool, contains) in enumerate(PROMPTS, 1):
        name = f"{number:02d}.wav"
        path = target / name
        if only_bad and path.is_file() and audio_ok(load_wav(path)):
            items.append({"file": name, "say": say, "tool": tool, "contains": contains})
            continue
        prompt(say, number, len(PROMPTS))
        audio = np.zeros(0, np.float32)
        for attempt in range(attempts):
            audio = recorder()
            if audio_ok(audio):
                break
            if retry is not None and attempt < attempts - 1:
                retry(say)
        save_wav(path, audio)
        recorded += 1
        items.append({"file": name, "say": say, "tool": tool, "contains": contains})
    (target / "index.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    return recorded


def load_items(data_dir: Path) -> list[Item]:
    index = folder(data_dir) / "index.json"
    if not index.is_file():
        return []
    return [Item(**entry) for entry in json.loads(index.read_text(encoding="utf-8"))]


def understood(text: str, known_app: Callable[[str], str | None]):
    """The direct command Jarvis would run for ``text`` (same steps as the conversation)."""
    from app.agent.language import detect_language, misheard_bangla_command, to_english_command
    from app.agent.shortcuts import match_shortcut, split_commands

    command = to_english_command(text) or text if detect_language(text) == "bn" else text
    call = match_shortcut(command, known_app, known_site=lambda s: s.lower() in ("youtube", "facebook", "gmail"))
    if call is None:
        parts = split_commands(command)
        if parts:
            call = match_shortcut(parts[0], known_app)
    if call is None and misheard_bangla_command(text):
        call = match_shortcut(misheard_bangla_command(text), known_app)
    return call


def run(data_dir: Path, transcribe: Callable[[np.ndarray], object], known_app: Callable[[str], str | None],
        out: Callable[[str], None] = print) -> dict:
    items = load_items(data_dir)
    if not items:
        out("No recordings yet: run --record-benchmark first.")
        return {}
    matched, similarity, times, missing = 0, [], [], 0
    lines = []
    per_item: dict[str, bool] = {}
    try:
        from app.voice.corrections import Corrections

        fixes = Corrections(data_dir)  # what --train-voice and "no, I said ..." taught it
    except Exception:
        fixes = None
    for item in items:
        audio = load_wav(folder(data_dir) / item.file)
        if not audio_ok(audio):
            lines.append(f"NO AUDIO {item.say!r:34} (the recording is empty or too quiet: run --record-benchmark "
                         "again; only these are re-recorded)")
            missing += 1
            continue
        started = time.perf_counter()
        transcript = transcribe(audio)
        ms = int((time.perf_counter() - started) * 1000)
        text = getattr(transcript, "text", "")
        call = understood(fixes.apply(text) if fixes is not None else text, known_app)
        ok = call is not None and call.name == item.tool and (
            not item.contains or item.contains in json.dumps(dict(call.arguments), ensure_ascii=False).lower())
        score = SequenceMatcher(None, item.say.lower(), text.lower()).ratio()
        matched += ok
        similarity.append(score)
        times.append(ms)
        lines.append(f"{'OK  ' if ok else 'MISS'} {item.say!r:34} heard {text!r:40} {ms:5d} ms  "
                     f"{call.name if call else '-'}")
        per_item[item.say] = bool(ok)
    tested = len(items) - missing
    if not tested:
        out("\n".join(lines))
        out("No usable recordings: run --record-benchmark again (speak after 'Speak now').")
        return {}
    result = {"items": tested, "missing": missing, "commands_ok": matched, "accuracy": round(100 * matched / tested, 1),
              "text_similarity": round(100 * float(np.mean(similarity)), 1), "avg_ms": int(np.mean(times)),
              "max_ms": int(np.max(times))}
    lines.append("")
    if missing:
        lines.append(f"{missing} recording(s) had no usable audio and were skipped.")
    lines.append(f"Commands understood: {matched}/{tested} ({result['accuracy']}%)   "
                 f"text similarity {result['text_similarity']}%   recognition {result['avg_ms']} ms average, "
                 f"{result['max_ms']} ms slowest")
    lines += compare_with_last(data_dir, result, per_item)
    save_history(data_dir, result, per_item)
    report = "\n".join(lines)
    for line in lines:
        out(line)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    (folder(data_dir) / f"report-{stamp}.txt").write_text(report + "\n", encoding="utf-8")
    return result


HISTORY = "history.jsonl"


def load_history(data_dir: Path) -> list[dict]:
    path = folder(data_dir) / HISTORY
    runs = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                runs.append(json.loads(line))
            except ValueError:
                continue
    return runs


def save_history(data_dir: Path, result: dict, per_item: dict[str, bool]) -> None:
    from app import __version__

    entry = {"at": datetime.now().isoformat(timespec="seconds"), "version": __version__, **result, "per_item": per_item}
    with open(folder(data_dir) / HISTORY, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def compare_with_last(data_dir: Path, result: dict, per_item: dict[str, bool]) -> list[str]:
    """What changed since the previous benchmark run: accuracy, speed, commands fixed or broken."""
    runs = load_history(data_dir)
    if not runs:
        return ["(First run: the next one will be compared with this.)"]
    last = runs[-1]
    lines = ["", f"Compared with the run of {last.get('at', '?')[:16].replace('T', ' ')} "
                 f"(version {last.get('version', '?')}):"]
    delta = result["accuracy"] - float(last.get("accuracy", 0))
    speed = result["avg_ms"] - int(last.get("avg_ms", 0))
    lines.append(f"  accuracy {delta:+.1f} points, recognition {speed:+d} ms")
    before = last.get("per_item", {})
    fixed = [s for s, ok in per_item.items() if ok and before.get(s) is False]
    broken = [s for s, ok in per_item.items() if not ok and before.get(s) is True]
    if fixed:
        lines.append("  now understood: " + ", ".join(repr(s) for s in fixed))
    if broken:
        lines.append("  NO LONGER understood: " + ", ".join(repr(s) for s in broken))
    if not fixed and not broken:
        lines.append("  the same commands work as before")
    return lines


# ------------------------------------------------------------------ voice training

_STOPWORDS = frozenset({"the", "a", "an", "is", "it", "to", "and", "i", "my", "me", "what", "how", "you", "of", "in",
                        "on", "up", "do", "open", "close", "play", "please", "now", "this", "that", "for"})


def _tokens(text: str) -> list[str]:
    import re

    return re.findall(r"[\wঀ-৿']+", text.lower())


def _bengali(text: str) -> bool:
    return any("ঀ" <= ch <= "৿" for ch in text)


def fix_pairs(heard: str, said: str) -> list[tuple[str, str]]:
    """What was misheard in ``heard`` and what it should have been, from one recording.

    "close phone lane" / "Close Phone Link" -> [("phone lane", "phone link")]. When the whole
    sentence came out in the wrong script ("chrome club" for "ক্রোম খোলো"), the whole short
    sentence is the fix."""
    from difflib import SequenceMatcher as _Matcher

    from app.utils.phonetic import similarity as sounds

    from app.voice.wake_word import is_background_noise

    a, b = _tokens(heard), _tokens(said)
    if not a or not b or is_background_noise(heard):  # "Thank you." from silence is not how you say anything
        return []
    if _bengali(said) != _bengali(heard) and not any(_bengali(w) for w in a) and len(a) <= 6:
        whole = " ".join(a)
        return [] if whole in _STOPWORDS or len(whole) < 4 else [(whole, said.strip(" ?!.।"))]
    pairs = []
    for op, i1, i2, j1, j2 in _Matcher(None, a, b, autojunk=False).get_opcodes():
        if op != "replace" or i2 - i1 > 4 or j2 - j1 > 4:
            continue
        if " ".join(a[i1:i2]) in _STOPWORDS or len(" ".join(a[i1:i2])) < 3:
            continue
        if i2 - i1 == 1:  # one word: keep a neighbour, so "love" -> "lock" only fixes "love my", not "I love it"
            if i1 > 0 and j1 > 0:
                i1, j1 = i1 - 1, j1 - 1
            elif i2 < len(a) and j2 < len(b):
                i2, j2 = i2 + 1, j2 + 1
        wrong, right = " ".join(a[i1:i2]), " ".join(b[j1:j2])
        if _bengali(wrong) == _bengali(right) and sounds(wrong, right) < 0.45:
            continue
        pairs.append((wrong, right))
    return pairs


def train(data_dir: Path, transcribe: Callable[[np.ndarray], object], known_app: Callable[[str], str | None],
          out: Callable[[str], None] = print) -> dict:
    """Learn how you pronounce your commands from the benchmark recordings.

    Each recording is recognized; where the command was not understood, the misheard words are
    compared with what you said and saved as corrections (the same store as "no, I said ...").
    A fix is kept only if it makes that command work and breaks none that worked before."""
    from app.voice.corrections import Corrections

    items = [i for i in load_items(data_dir) if audio_ok(load_wav(folder(data_dir) / i.file))]
    if not items:
        out("No recordings yet: run --record-benchmark first.")
        return {}
    store = Corrections(data_dir)

    def works(item: Item, text: str) -> bool:
        call = understood(store.apply(text), known_app)
        return call is not None and call.name == item.tool and (
            not item.contains or item.contains in json.dumps(dict(call.arguments), ensure_ascii=False).lower())

    heard = []
    for item in items:
        text = getattr(transcribe(load_wav(folder(data_dir) / item.file)), "text", "") or ""
        heard.append((item, text))
    before = sum(works(item, text) for item, text in heard)
    learned: list[tuple[str, str]] = []
    for item, text in heard:
        if not text or works(item, text):
            continue
        for wrong, right in fix_pairs(text, item.say):
            if wrong in store.items():
                continue
            store.learn(wrong, right)
            others_ok = all(works(i, t) for i, t in heard
                            if i is not item and works_without(store, wrong, i, t, works))
            if works(item, text) and others_ok:
                learned.append((wrong, right))
                out(f"  learned  {wrong!r} -> {right!r}")
            else:
                store.forget(wrong)
    after = sum(works(item, text) for item, text in heard)
    out(f"\nLearned {len(learned)} fix(es). Commands understood: {before}/{len(heard)} before, {after}/{len(heard)} now.")
    return {"learned": len(learned), "before": before, "after": after, "items": len(heard)}


def works_without(store, wrong: str, item: Item, text: str, works: Callable[[Item, str], bool]) -> bool:
    """Did ``item`` work before ``wrong`` was learned? (Only those must keep working.)"""
    meant = store.items().pop(wrong)
    try:
        return works(item, text)
    finally:
        store.items()[wrong] = meant
