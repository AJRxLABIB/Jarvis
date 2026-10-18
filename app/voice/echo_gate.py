"""Telling YOUR voice apart from Jarvis's own voice coming back from the speakers - so you can just talk while it speaks.

Jarvis knows exactly how loud it is playing at every moment. Whatever the microphone hears while it plays is, mostly,
that sound coming back ("echo"), a fixed number of decibels quieter or louder depending on your speakers, microphone
and room. The gate learns that difference ("coupling") continuously and only calls it YOU when the microphone is
clearly louder than the echo can explain AND the voice detector hears speech, for a few frames in a row (~0.25 s).
Between sentences (Jarvis silent) any clear speech counts at once.
"""

from __future__ import annotations

from collections import deque

import numpy as np

SILENT_DB = -65.0          # playback quieter than this = Jarvis is not making sound right now


class EchoGate:
    WARMUP_FRAMES = 20          # ~1.6 s of hearing itself before it trusts the estimate while it plays

    def __init__(self, margin_db: float = 10.0, onset_frames: int = 4, floor_db: float = -45.0,
                 start_coupling: float = -8.0, max_coupling: float = 6.0, min_coupling: float = -40.0) -> None:
        self.margin_db, self.onset_frames, self.floor_db = margin_db, onset_frames, floor_db
        self.max_coupling, self.min_coupling = max_coupling, min_coupling
        self.coupling = start_coupling          # learned: microphone dB minus playback dB for pure echo
        self._history: deque[float] = deque(maxlen=100)  # ~8 s of 80 ms frames, kept from one answer to the next
        self.run = 0
        self.extra_margin = 0.0                 # raised after a false alarm, decays again
        self.learned = 0

    def expected_echo(self, playback_db: float) -> float:
        return -120.0 if playback_db <= SILENT_DB else playback_db + self.coupling

    def feed(self, mic_db: float, playback_db: float, speech: bool) -> bool:
        """One microphone frame. True once the user has clearly been speaking for ``onset_frames`` frames."""
        playing = playback_db > SILENT_DB
        if playing and self.learned < self.WARMUP_FRAMES:
            self._learn(mic_db, playback_db)
            self.run = 0
            return False                         # still learning how loud its own voice comes back
        expected = self.expected_echo(playback_db)
        threshold = max(self.floor_db, expected + self.margin_db + self.extra_margin)
        voice = speech and mic_db > threshold
        self.run = self.run + 1 if voice else 0
        if playing and self.run == 0:
            # learn from every frame while it plays - its words AND the gaps between them (learning only from the
            # quiet gaps made the echo look 60 dB quieter than it is, so its own words counted as you)
            self._learn(mic_db, playback_db)
        self.extra_margin = max(0.0, self.extra_margin - 0.02)          # a false alarm is forgotten in ~20 s
        return self.run >= self.onset_frames

    def _learn(self, mic_db: float, playback_db: float) -> None:
        self._history.append(mic_db - playback_db)
        self.learned += 1
        if len(self._history) >= 6:
            value = float(np.percentile(self._history, 95))           # the loudest echo, not the average
            self.coupling = min(self.max_coupling, max(self.min_coupling, value))

    def false_alarm(self) -> None:
        """It was its own voice / the TV after all: be harder to convince for a while."""
        self.extra_margin = min(9.0, self.extra_margin + 4.0)

    def reset(self) -> None:
        self.run = 0
