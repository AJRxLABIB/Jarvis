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
