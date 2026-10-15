# Verification: the computer agent and the developer pipeline

Two ways Jarvis checks its own work instead of assuming it worked.

## Computer agent: before/after screen verification

Code: `app/core/screen_verify.py`, used by `app/tools/screen_agent.py`.

Every action that drives the screen - `click_text`, `click_thing`, `type_text`, `press_keys`, `scroll_screen` -
takes a cheap snapshot before and after and compares them:

* a small grey thumbnail of the screen (pixel change),
* the active window title (did the foreground change - e.g. alt+tab),
* for typing, whether the text shows up (when the screen was read).

The result says what really happened: **verified** (the screen changed / the text appeared / the window
switched), **no visible effect** ("nothing changed, it may not have worked"), or **couldn't check** (no capture
was possible). Jarvis never reports a screen action as successful without this check, and never turns "couldn't
check" into a false success. The comparison logic is pure and unit-tested; snapshots are held in memory only and
never saved.

## Developer pipeline: test-gated edits

Code: `app/core/dev_pipeline.py`, tool `safe_edit` in `app/tools/code_tools.py`.

`safe_edit` changes code with a safety net:

1. run the project's own test command for a **baseline**;
2. apply the edits (each backed up, as `edit_code` does);
3. run the tests **again**;
4. **keep** the change only if the tests still pass and no test was lost - otherwise roll every edit back.

"No test was lost" is the anti-cheat: making the suite green by deleting, skipping or renaming tests away counts
as a **regression**, so a change can never "pass" by weakening the tests. If there's no baseline (the test command
won't run cleanly), `safe_edit` refuses to make a change it can't verify. Test output is treated as untrusted data
- only the pass/fail numbers are used, never anything in it as an instruction.

This complements the self-improvement pipeline (`propose_code_change` -> `test_proposal` -> `apply_proposal`,
which tests Jarvis's OWN code on a copy before applying): `safe_edit` is the same idea for your projects.
