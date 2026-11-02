# Jarvis's voice

Code: `app/tts/jarvis_voice.py`, `app/tts/manager.py` (`create_engine`, engine `jarvis`), `app/tts/neural.py`.

The only engine offered in Settings and the setup wizard is **JARVIS**: an original JARVIS-style voice - a calm British
male voice (Microsoft `en-GB-RyanNeural`, about 6% slower and a little lower) with a light processing finish (low cut,
a 6 ms shimmer, faint short reflections, peak-limited) that gives it a subtle "AI" character. Offline, the same profile
uses Kokoro `bm_george`.

**It is not the film character's actor's voice.** Cloning a real person's voice from movie audio without their consent
is not something this project does, so no such model or recording is shipped or downloaded.

Fallbacks, used automatically and not offered as choices: Bangla text uses the Bangla voice (the British voice cannot
speak Bangla); with no internet the offline engine (whichever the "auto" chain finds: Kokoro, Piper or Windows SAPI) speaks with the same effect.
Speed, volume and the Bangla voice stay in Settings -> Voice.

## Hearing quality (round 51)

What was wrong, found by reading the capture path (not yet measured on a real microphone):

* **Aliasing**: the old resampler had no low-pass filter. Microphones that open at 44.1 kHz (and 48 kHz, via block averaging) folded
  everything above 8 kHz (hiss, fan noise, consonant harshness) back into the speech band, which blurs sounds like s/f/t.
  Now `app/voice/audio.py::StreamResampler` low-passes first (windowed-sinc, 161 taps), then interpolates.
* **Seams**: hands-free listening converted each 30 ms block separately, leaving a tiny click every block. The stream now carries
  filter state and position across blocks.
* **Rumble and DC offset**: desk thumps, fan and mains hum (below ~80 Hz) fed the recogniser. `remove_rumble` applies a zero-phase
  80 Hz high-pass and removes DC before every transcription.
* **One click set the gain**: the level boost used the single loudest sample, so a pop left quiet speech quiet. It now uses the
  99.5th percentile when a spike is more than 3x above it.
* **Clipped word starts**: pre-roll before the detected start of speech raised from 0.3 s to 0.45 s.

Check on your PC: `python run.py --devices` (note the sample rate the mic opens at), then say the same ten commands before and
after with `--record-benchmark` / `--benchmark`. Still true: a cheap or distant microphone limits accuracy more than any software;
speak 15-30 cm from it, and keep the room's TV/speakers off or use the echo gate. Tests: `tests/test_hearing_quality.py`.
