# Hardware validation (round 55)

The automated tests run on a Linux sandbox with fake hardware. They cannot show that UI Automation, display scaling, several monitors, your microphone,
your speakers or your phone work. `python run.py --validate <area>` runs the real checks **on your PC** and saves the result in `data\validation`.
`python run.py --validate status` shows the last result of each area; an area never run says **NEVER RUN**. A check that cannot run is recorded as
**PENDING** with the reason and never counts as passed; the verdict is PASSED only when nothing failed and nothing was left pending.

| Command | What it does | Needs |
|---|---|---|
| `--validate windows` | DPI awareness, monitors and scaling, screenshot size = real screen size | Windows |
| `--validate windows --launch` | also opens Notepad: window rectangle from three sources must agree (DPI mismatch detector), UI Automation reads the window (element count, time), focus works, the window can be placed on every monitor; Notepad is closed afterwards (nothing typed or saved) | Windows, 1+ monitors |
| `--validate voice` | voice-detector reaction (clipped word starts), end-of-speech latency (3 trials, real detector), time to first spoken audio, speaker stop latency (interruption), "does Jarvis hear itself?", and the per-language accuracy of your last `--benchmark` | microphone, speakers |
| `--validate phone` | trusted phone, recent check-in, app version, control allowed, then real actions (show message, flashlight on/off) with **you** confirming the effect; a success report you cannot see is recorded as a FALSE SUCCESS | paired phone |
| `--validate phone --full` | also rings the phone for 3 s and measures how fast it checks in again after you switch Wi-Fi off and on | paired phone |
| `--validate all` | the three above | - |

Never sent: calls, SMS, lock, screen reading, taps.

## Bangla accuracy
`--record-benchmark` now also records 14 recognition-only prompts (Bangla numbers and dates, technical words, names, Bangla-English code-switching,
English technical phrases); earlier recordings are kept. `--benchmark` reports **word error and character error per language** (Bangla, mixed, English) and a
verdict that never says more than the data supports: fewer than 8 recordings of a language is "NO CLAIM"; "reliable" needs character error <= 12% and command
accuracy >= 90%. These are *your* voice and microphone; a few dozen phrases are a small sample, not a general accuracy figure.

## Manual phone checklist (cannot be automated)
Listed as PENDING by `--validate phone`: screen-off check-ins for 30 minutes with battery optimisation off; revoke then re-pair; forwarded notifications arrive and codes
are hidden; Share > JARVIS Mobile lands in the PC's phone folder; emergency stop from the phone during a PC action. Note each result next to the date in this file.

## Honest limits
The checks' decisions are tested with fake hardware (`tests/test_validation.py`, 25 tests). `RealEnv`, `RealVoiceEnv` and `RealPhoneEnv` are the only code that touches real
hardware and have NOT been run by me; expect to meet a first-run bug there and report it with the saved JSON. Thresholds (e.g. detector reaction <= 250 ms, speaker stop <= 300 ms,
three-pixel rectangle tolerance) are starting points, not standards.
