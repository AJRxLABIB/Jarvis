"""Personal wake-word check: your own recordings of "Hey Jarvis" confirm each wake-up.

Menu -> Teach me my wake word records the wake phrase five times. When the wake-word model
fires, the last 1.6 seconds of audio are compared with those recordings (dynamic time warping
over log-mel features, the classic template method). Audio that doesn't sound like your wake
phrase - a TV line, a similar word - is ignored. This adds a check on top of the wake-word
model; it does not replace it. Only the features are stored (data\\wake_templates.npz), not audio.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

FILE = "wake_templates.npz"
MIN_TEMPLATES = 3
MARGIN = 1.25  # accept up to this much worse than your recordings match each other
N_MELS = 40
BUFFER_SECONDS = 1.6


def features(audio: np.ndarray) -> np.ndarray:
    """Log-mel frames (20 ms steps). Each frame is centred and scaled to unit length on its own, so the
    result doesn't depend on how much silence is around the phrase or how loud it was."""
    from app.voice.speaker import _mel_filters

    wave = np.asarray(audio, dtype=np.float64).reshape(-1) * 32768.0
    frame, hop, n_fft = 400, 320, 512
    if wave.size < frame:
        wave = np.pad(wave, (0, frame - wave.size))
    count = 1 + (wave.size - frame) // hop
    index = np.arange(frame)[None, :] + hop * np.arange(count)[:, None]
    frames = wave[index]
    frames = frames - frames.mean(axis=1, keepdims=True)
    frames *= np.hamming(frame)[None, :]
    power = np.abs(np.fft.rfft(frames, n=n_fft)) ** 2
    feats = np.log(power @ _mel_filters(N_MELS, n_fft).T + 1.0)  # +1: silence stays near zero, not -inf
    feats -= feats.mean(axis=1, keepdims=True)
    norms = np.linalg.norm(feats, axis=1, keepdims=True)
    return (feats / np.maximum(norms, 1e-6)).astype(np.float32)


def trim(audio: np.ndarray, rate: int = 16000, floor_db: float = 35.0) -> np.ndarray:
    """Cut the silence before and after the spoken phrase."""
    audio = np.asarray(audio, np.float32).reshape(-1)
    hop = rate // 100
    if audio.size < hop * 3:
        return audio
    frames = audio[: audio.size // hop * hop].reshape(-1, hop)
    level = 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-9)
    loud = np.where(level > level.max() - floor_db)[0]
    if loud.size == 0:
        return audio
    start, end = max(0, loud[0] - 5), min(frames.shape[0], loud[-1] + 6)
    return audio[start * hop:end * hop]


def match_cost(template: np.ndarray, stream: np.ndarray) -> float:
    """Subsequence DTW: the best-matching stretch of ``stream`` for the whole ``template``.
    Average cosine distance along the path (0 = identical, larger = less alike)."""
    if template.size == 0 or stream.size == 0:
        return float("inf")
    cost = 1.0 - template @ stream.T  # (T, N) cosine distances
    rows, cols = cost.shape
    acc = np.full((rows, cols), np.inf, dtype=np.float64)
    acc[0] = cost[0]  # the template may start anywhere in the stream
    for i in range(1, rows):
        prev = acc[i - 1]
        diag = np.concatenate([[np.inf], prev[:-1]])
        best = np.minimum(prev, diag) + cost[i]
        # horizontal steps (stream moves, template waits) need a running minimum
        row = best.copy()
        for j in range(1, cols):
            candidate = row[j - 1] + cost[i, j]
            if candidate < row[j]:
                row[j] = candidate
        acc[i] = row
    return float(acc[-1].min() / (rows + 0.5 * rows))


class WakeTemplates:
    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / FILE
        self.templates: list[np.ndarray] = []
        self.threshold = 0.0
        self._load()

    @property
    def ready(self) -> bool:
        return len(self.templates) >= MIN_TEMPLATES and self.threshold > 0

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            import io

            from app.security.protect import read_bytes

            data = np.load(io.BytesIO(read_bytes(self.path)), allow_pickle=False)
            count = int(data["count"])
            self.templates = [data[f"t{i}"] for i in range(count)]
            self.threshold = float(data["threshold"])
        except Exception:
            self.templates, self.threshold = [], 0.0

    def enroll(self, recordings: list[np.ndarray]) -> float:
        """Save templates from your recordings of the wake phrase; returns the acceptance threshold."""
        trimmed = [trim(r) for r in recordings if np.asarray(r).size >= 16000 * 0.3]
        trimmed = [a for a in trimmed if a.size >= 16000 * 0.25]
        if len(trimmed) < MIN_TEMPLATES:
            raise ValueError(f"I need at least {MIN_TEMPLATES} clear recordings of the wake phrase.")
        clips = [features(a) for a in trimmed]
        worst = 0.0
        quiet = np.random.default_rng(0).normal(0, 1e-4, 4000).astype(np.float32)
        for i, audio in enumerate(trimmed):  # how well each recording is matched by the others, as in live audio
            stream = features(np.concatenate([quiet, audio, quiet]))
            best = min(match_cost(other, stream) for j, other in enumerate(clips) if j != i)
            worst = max(worst, best)
        self.templates, self.threshold = clips, worst * MARGIN + 0.02
        self.path.parent.mkdir(parents=True, exist_ok=True)
        arrays = {f"t{i}": c for i, c in enumerate(clips)}
        import io

        from app.security.protect import write_bytes

        buffer = io.BytesIO()
        np.savez(buffer, count=np.array(len(clips)), threshold=np.array(self.threshold), **arrays)
        write_bytes(self.path, buffer.getvalue())  # encrypted for your Windows account
        return self.threshold

    def score(self, audio: np.ndarray) -> float:
        stream = features(np.asarray(audio, np.float32)[-int(16000 * BUFFER_SECONDS):])
        return min(match_cost(t, stream) for t in self.templates) if self.templates else float("inf")

    def verify(self, audio: np.ndarray) -> bool:
        """True if the audio contains something that sounds like your wake phrase (True when not set up)."""
        if not self.ready:
            return True
        return self.score(audio) <= self.threshold

    def forget(self) -> None:
        self.path.unlink(missing_ok=True)
        self.templates, self.threshold = [], 0.0
