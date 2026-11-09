"""Who spoke when, roughly: "Speaker 1 / Speaker 2" turns for a recording, computed locally from the audio. Used by the media index (core/media_index.py).

HOW IT WORKS (deliberately simple, no model download): the audio is cut into 2 s windows (every 1 s). For each window with enough voiced sound it takes a small
fingerprint - the average spectral shape (13 mel-cepstral numbers, gain-independent) and the pitch (autocorrelation) - and the windows are grouped by
average-linkage clustering: windows closer than ``threshold`` end up in one group. Each group becomes a speaker, numbered by first appearance. Runs shorter than
``min_turn_s`` are absorbed into the neighbouring turn, and silence ends a turn.

WHAT IT IS NOT: it does not know WHO anyone is (labels are only "Speaker N" for this recording), it cannot separate people talking at the same time, and it mixes
up similar voices (two men of similar pitch and microphone). Noise, music and a changing microphone distance split one person into several. The threshold was tuned on
SYNTHETIC voices only; real recordings are unverified, so treat it as a convenience for finding turns, never as evidence of who said something.

Pure numpy. ``windows_from_wav`` streams a WAV so a long meeting does not need to fit in memory.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass
from typing import Callable, Iterator, Sequence

import numpy as np

RATE = 16000
WIN_S, HOP_S = 2.0, 1.0
N_FFT, FRAME, HOP = 512, 400, 160
N_MEL, N_CEP = 24, 13
PITCH_WEIGHT = 3.0               # log2(f0) counts for this many cepstral units: pitch is the strongest cue between two different voices
THRESHOLD = 10.0                 # average-linkage distance above which two groups are different speakers; a guess for real voices (synthetic ones are 1 vs 90 apart), adjustable by "sensitivity"
MIN_TURN_S = 3.0
MAX_SPEAKERS = 6
MAX_WINDOWS = 12000              # about 3 h; longer audio is cut off here rather than taking forever
MIN_VOICED = 0.35                # share of a window's frames that must carry sound for it to count


def _mel_bank() -> np.ndarray:
    def hz_to_mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def mel_to_hz(m):
        return 700.0 * (10 ** (m / 2595.0) - 1.0)

    points = mel_to_hz(np.linspace(hz_to_mel(100.0), hz_to_mel(4000.0), N_MEL + 2))
    bins = np.floor((N_FFT + 1) * points / RATE).astype(int)
    bank = np.zeros((N_MEL, N_FFT // 2 + 1))
    for i in range(1, N_MEL + 1):
        lo, mid, hi = bins[i - 1], bins[i], bins[i + 1]
        for k in range(lo, mid):
            bank[i - 1, k] = (k - lo) / max(1, mid - lo)
        for k in range(mid, hi):
            bank[i - 1, k] = (hi - k) / max(1, hi - mid)
    return bank


_BANK = _mel_bank()
_WINDOW = np.hamming(FRAME)
_DCT = np.cos(np.pi / N_MEL * (np.arange(N_MEL) + 0.5)[None, :] * np.arange(1, N_CEP)[:, None])     # c1..c12 (c0 = loudness: dropped)


def embed(window: np.ndarray) -> np.ndarray | None:
    """Fingerprint of one window of 16 kHz mono float samples, or None when there is not enough voiced sound in it."""
    x = np.asarray(window, dtype=np.float64)
    if len(x) < FRAME * 4:
        return None
    n = 1 + (len(x) - FRAME) // HOP
    idx = np.arange(FRAME)[None, :] + HOP * np.arange(n)[:, None]
    frames = x[idx]
    energy = (frames ** 2).mean(axis=1)
    peak = float(np.percentile(energy, 95))
    if peak < 1e-7:
        return None
    voiced = energy > 0.1 * peak
    if voiced.mean() < MIN_VOICED:
        return None
    spec = np.abs(np.fft.rfft(frames[voiced] * _WINDOW, N_FFT)) ** 2
    logmel = np.log(spec @ _BANK.T + 1e-10).mean(axis=0)
    cep = _DCT @ logmel
    f0 = _pitch(frames[voiced])
    if f0 is None:
        return None
    return np.concatenate([cep, [PITCH_WEIGHT * np.log2(f0)]])


def _pitch(frames: np.ndarray) -> float | None:
    """Median fundamental frequency (Hz) over the frames that are clearly periodic; None when none are."""
    lo, hi = int(RATE / 400), int(RATE / 70)
    found = []
    for f in frames[:: max(1, len(frames) // 40)]:
        f = f - f.mean()
        r = np.correlate(f, f, "full")[len(f) - 1:]
        if r[0] <= 1e-9:
            continue
        seg = r[lo:hi]
        k = int(np.argmax(seg))
        if seg[k] / r[0] > 0.3:
            found.append(RATE / (lo + k))
    return float(np.median(found)) if len(found) >= 3 else None


def cluster(vectors: Sequence[np.ndarray], threshold: float = THRESHOLD, max_speakers: int = MAX_SPEAKERS) -> list[int]:
    """Average-linkage grouping. -> one label per vector (0, 1, ...), numbered by first appearance. Groups that stay apart are at least ``threshold`` away."""
    n = len(vectors)
    if n == 0:
        return []
    v = np.asarray(vectors, dtype=np.float64)
    groups = [[i] for i in range(n)]
    d = np.linalg.norm(v[:, None, :] - v[None, :, :], axis=2)
    while len(groups) > 1:
        best, pair = None, (0, 1)
        for a in range(len(groups)):
            for b in range(a + 1, len(groups)):
                dist = float(d[np.ix_(groups[a], groups[b])].mean())
                if best is None or dist < best:
                    best, pair = dist, (a, b)
        if best is not None and best > threshold and len(groups) <= max_speakers:
            break
        a, b = pair
        groups[a] += groups[b]
        del groups[b]
    labels = [0] * n
    for g, members in enumerate(sorted(groups, key=min)):
        for m in members:
            labels[m] = g
    return labels


MIN_GROUP = 3        # a group of fewer windows than this is a blend of two voices at a hand-over (or a cough), not a speaker


def _absorb_small_groups(vectors: Sequence[np.ndarray], labels: list[int]) -> list[int]:
    """Windows in tiny groups join the nearest big group (the window where one person stops and the next starts sounds like neither); renumbered by first appearance."""
    counts: dict[int, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    big = [g for g, n in counts.items() if n >= MIN_GROUP]
    if not big or len(big) == len(counts):
        return labels
    v = np.asarray(vectors, dtype=np.float64)
    centres = {g: v[[i for i, label in enumerate(labels) if label == g]].mean(axis=0) for g in big}
    out = [label if label in centres else min(centres, key=lambda g: float(np.linalg.norm(v[i] - centres[g]))) for i, label in enumerate(labels)]
    order = {g: n for n, g in enumerate(dict.fromkeys(out))}
    return [order[g] for g in out]


@dataclass(frozen=True)
class Turn:
    start: float
    end: float
    speaker: int            # 0-based; shown as "Speaker 1"

    @property
    def name(self) -> str:
        return f"Speaker {self.speaker + 1}"


def turns_from_windows(embeddings: Sequence[np.ndarray | None], threshold: float = THRESHOLD, win_s: float = WIN_S, hop_s: float = HOP_S,
                       min_turn_s: float = MIN_TURN_S) -> list[Turn]:
    """Windows (None = silence / not enough voice) -> turns."""
    real = [(i, e) for i, e in enumerate(embeddings) if e is not None]
    if not real:
        return []
    vectors = [e for _, e in real]
    labels = _absorb_small_groups(vectors, cluster(vectors, threshold))
    per_window: list[int | None] = [None] * len(embeddings)
    for (i, _), label in zip(real, labels):
        per_window[i] = label
    # smooth lone flips: a window whose neighbours agree with each other takes their label
    for i in range(1, len(per_window) - 1):
        a, b = per_window[i - 1], per_window[i + 1]
        if a is not None and a == b and per_window[i] != a:
            per_window[i] = a
    turns: list[Turn] = []
    i = 0
    while i < len(per_window):
        label = per_window[i]
        if label is None:
            i += 1
            continue
        j = i
        while j + 1 < len(per_window) and per_window[j + 1] == label:
            j += 1
        pad = (win_s - hop_s) / 2                      # a window speaks for the middle of itself, so neighbouring turns meet instead of overlapping
        turns.append(Turn(0.0 if i == 0 else i * hop_s + pad, (j + 1) * hop_s + pad, label))
        i = j + 1
    return _absorb_short(turns, min_turn_s)


def _absorb_short(turns: list[Turn], min_turn_s: float) -> list[Turn]:
    """A turn shorter than ``min_turn_s`` that sits between (or next to) another speaker is joined to the previous turn; neighbours of one speaker merge."""
    out: list[Turn] = []
    for t in turns:
        if out and t.end - t.start < min_turn_s and t.start - out[-1].end <= 1.5 * HOP_S:
            out[-1] = Turn(out[-1].start, max(out[-1].end, t.end), out[-1].speaker)
        elif out and out[-1].speaker == t.speaker and t.start - out[-1].end <= 2.0 * HOP_S:
            out[-1] = Turn(out[-1].start, t.end, t.speaker)
        else:
            out.append(t)
    return out


def windows_from_wav(path, win_s: float = WIN_S, hop_s: float = HOP_S, max_windows: int = MAX_WINDOWS,
                     progress: Callable[[float], None] = lambda frac: None) -> Iterator[np.ndarray | None]:
    """Streams a 16 kHz mono 16-bit WAV, yielding one embedding (or None) per window."""
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("expected a 16 kHz mono 16-bit WAV")
        total = w.getnframes()
        win, hop = int(win_s * RATE), int(hop_s * RATE)
        buf = np.zeros(0, dtype=np.float32)
        count = 0
        while count < max_windows:
            need = hop if len(buf) >= win else win - len(buf)
            raw = w.readframes(need)
            if not raw:
                break
            buf = np.concatenate([buf, np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0])
            if len(buf) < win:
                break
            yield embed(buf[:win])
            buf = buf[hop:]
            count += 1
            if count % 200 == 0:
                progress(min(1.0, w.tell() / max(1, total)))


def diarize_wav(path, threshold: float = THRESHOLD, cancelled: Callable[[], bool] = lambda: False) -> list[Turn]:
    embeddings: list[np.ndarray | None] = []
    for e in windows_from_wav(path):
        if cancelled():
            raise InterruptedError("cancelled")
        embeddings.append(e)
    return turns_from_windows(embeddings, threshold)


def speaker_at(turns: Sequence[Turn], start: float, end: float) -> tuple[str, float]:
    """The speaker who covers most of [start, end] and their share of the covered time; ("", 0.0) when no turn overlaps."""
    cover: dict[int, float] = {}
    for t in turns:
        overlap = min(end, t.end) - max(start, t.start)
        if overlap > 0:
            cover[t.speaker] = cover.get(t.speaker, 0.0) + overlap
    if not cover:
        return "", 0.0
    best = max(cover, key=lambda k: cover[k])
    return f"Speaker {best + 1}", cover[best] / sum(cover.values())


def annotate(segments: Sequence[tuple[float, float, str]], turns: Sequence[Turn]) -> list[tuple[float, float, str, str]]:
    """Each text segment with its likely speaker. A segment shared by several speakers (the recognizer's 30 s chunks often are) lists the main one with ``+`` for
    others: "Speaker 1 (+Speaker 2)"; nothing is guessed where no voice was found."""
    out = []
    for a, b, text in segments:
        names: dict[str, float] = {}
        for t in turns:
            o = min(b, t.end) - max(a, t.start)
            if o > 0:
                names[t.name] = names.get(t.name, 0.0) + o
        if not names:
            out.append((a, b, text, ""))
            continue
        ordered = sorted(names, key=lambda k: -names[k])
        label = ordered[0] + (f" (+{', '.join(ordered[1:3])})" if len(ordered) > 1 and names[ordered[1]] >= 0.15 * sum(names.values()) else "")
        out.append((a, b, text, label))
    return out
