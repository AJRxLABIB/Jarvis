# Round 84 - the two opt-in recorders

Both were held back earlier as privacy decisions. They are built in the narrowest useful form and are OFF until you act.

## Screen history (`screen_history` tool, `core/screen_history.py`, migration 47)

* Keeps TEXT only (OCR of the window in front) with title, program and time. Never a picture.
* Two switches plus a start: `SCREEN_CAPTURE_ENABLED=true` and `SCREEN_HISTORY_ENABLED=true` in `.env`, then "start screen history" (confirmed). It does not
  restart by itself when Jarvis starts.
* Captures at most every 20 s, only when the window in front changed or 3 minutes passed; identical text is not stored twice; each text is cut to 4000 characters.
* Skipped: everything the capture layer already blocks (password managers, banking, private browsing, meetings, your privacy zones, blocked windows), Jarvis's
  own window, and any window while a password field has the focus. Secret-looking text is replaced before storing.
* Kept `SCREEN_HISTORY_DAYS` (default 7). Search, status, stop, delete everything. The capture indicator is on while it records.
* Limits: OCR errors mean a search can miss; text in a blocked window is never there to find; search is by words, not meaning.

## Click teaching (`demo_record` tool, `core/demo_record.py`)

* During a session you start (confirmed, at most 5 minutes): for each LEFT click, the program in front and the name and type of the control under the pointer.
* No keyboard hook exists: keys, typed text, passwords and shortcuts are never seen. No pointer movement, no pictures, no right clicks or drags.
* Dropped: password fields, clicks inside a privacy zone, clicks on Jarvis, repeated clicks, controls with no name (counted, cannot be replayed).
* Stop shows a draft: "open <program>", "press <control>" steps. Nothing is saved until you say save (confirmed); the saved workflow replays through the normal
  tools, so each press still asks. Neither recorder can be started from inside an automation.
* Limits: a routine that needs typing cannot be captured this way - add those steps by telling Jarvis. Control names differ between windows and app versions,
  so a replay can fail to find a control; try a saved routine once while watching.

Tests: `tests/test_round84.py` (fakes). Unverified on Windows: the real OCR loop, the password-focus check, the click poller and UI Automation lookups.
