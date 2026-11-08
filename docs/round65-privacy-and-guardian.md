# Round 65: privacy zones, guardian USB / file integrity, adaptive interaction

Unit-tested (`tests/test_round65.py`, 17 tests). **Not run on your real PC; the Windows readers are unverified.**

## Privacy zones (`core/privacy_zones.py`, tool `privacy_zones`)
Wider than the existing `SCREEN_BLOCKED_WINDOWS` screenshot block (which stays). A zone restricts any of: `capture`, `memory`, `clipboard`, `remote`, `index`, while a matching program / window title is in front.
Built in: password managers (all kinds), banking and payments (all except nothing - all kinds), private browsing (capture, memory, index), meetings (capture, memory, remote, index; uses the meeting detector). Add your own (title words / program names, which kinds), switch a built-in off (asks first).
Enforced in: the tool executor (screen tools refuse), conversation memory / habits / memory suggestions (5 places), clipboard history, and the phone protocol (`screen`, `mouse`, `key` get a 403 `privacy_zone`). A zone only restricts; nothing here grants a permission. `status` says when the foreground window cannot be read (then only the existing screen block applies).
Limits: decided every 2 s from the foreground program + title (a background sensitive window is not a zone unless the existing screen block sees it); `index` is recorded for the knowledge indexers but no indexer reads it yet (stated, not hidden).

## Guardian additions (`core/guardian.py`)
* **USB**: a new removable drive since the baseline = a low-severity incident ("if it is yours, accept it"). First look only records. Identity = letter + file system + size (+ volume serial when Windows gives it); contents are never read.
* **File integrity**: `GUARDIAN_INTEGRITY_FILES` (`;`-separated, e.g. the hosts file): the SHA-256 is compared with the accepted baseline; changed or removed = a medium incident with sizes (never contents). Only hashes, sizes and times are stored. A reported change stays "changed" until you accept the baseline. Max 50 files, 50 MB each.
Neither detector acts.

## Adaptive interaction (`core/interaction.py`, tool `interaction_advice`)
Ranks voice / keyboard / gestures / phone and says whether replies should be spoken, from: meeting, full-screen app, mode, typing, microphone, gesture setting, phone connection. Advice only.

## Not built (and why)
Android sensors and camera screens (no JDK here); eye-gaze / head-pose (research item: needs measured accuracy on your webcam first); embedded llama.cpp runtime (needs benchmarks on your RTX 4050 before replacing Ollama); an `index`-zone consumer; real-device validation.
