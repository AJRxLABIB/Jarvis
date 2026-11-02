# Round 53: tool foundry, workspaces, retrieval sources, multilingual set, APK and key helpers

**Tool foundry** (`build_tool`, `app/core/foundry.py`): a new tool is a *parameterised workflow of existing tools* - never generated code. From a request
(local model drafts it) or a JSON spec: validate (names, inputs, every step's tool and arguments, forbidden tools, blocked risk) -> preview (what-if:
risk, paths, collisions, undo) -> test (real scheduling/recovery logic against mock tools) -> save, only through the normal workflow path (asks on screen).
`plan` saves nothing. It cannot grant itself permissions; each step still asks for its own when it runs. A passing mock test shows the plan is well-formed,
not that real applications cooperate. Tests: `tests/test_foundry.py`.

**Workspaces** (`workspace`, `app/core/workspaces.py`): one named place = project folder + files + saved window layout + goal (+ its unfinished mission) + notes.
`show` previews what restoring would open and lists what is missing, before anything runs; `restore` runs ordinary `open_path` / `arrange_windows` calls
through the executor (permissions, confirmation, audit apply). Signals (files, layout, goal, mission) can be switched off per workspace; that is only a
switch, not learned as a preference about you. Resuming a paused mission is left to you. Tests: `tests/test_workspaces.py`.

**Retrieval sources** (`retrieval_sources`, `app/memory/sources.py`): exclusion patterns (applied when indexing *and* when answering, plus `purge` of what was
already indexed), switches to leave memories / documents / conversations out of the combined search, readers for calendar `.ics`, saved `.html`, Google Keep
JSON and `.rst`, and near-duplicate detection (word-shingle similarity: copies and light edits, not paraphrases). The combined search no longer cites the same
text twice. Nothing is deleted by duplicate detection. Tests: `tests/test_sources.py`.

**Multilingual set** (`app/agent/multilingual_eval.py`, `python -m app.agent.multilingual_eval`, lab suite "multilingual"): 61 labelled cases (Bangla script,
romanised Bangla, English; risky, vague, unsafe, negation, Unicode-variant and held-out variants). Reports detection, command accuracy, risky-command accuracy
and **unsafe rewrites (must be 0)**. Building it found and fixed two real defects in `app/agent/language.py`: a negated command ("ক্রোম বন্ধ করো না" = do NOT
close Chrome) was turned into "close chrome", and apps whose name contains "অ্যাপ" (WhatsApp) were not understood. Measures the deterministic command layer only,
not Bangla speech recognition; the set is small and developer-written. Tests: `tests/test_multilingual.py`.

**APK and signing key** (`python scripts/signing_key.py status | plan | new-key --yes`, `app/remote/apk_check.py`): the bundled `JarvisMobile.apk` reports 1.4.0,
matches the source manifest and `MOBILE_APP_VERSION`, and no source file is newer than it, so it is current; it could not be rebuilt here (no Android SDK, downloads
blocked), so rebuild on the PC with `packaging\build_android.ps1` after any Android source change. The signing key is *not* tracked now but IS in two commits of the
history; `plan` lays out keep / rotate / scrub honestly. The decision is yours; nothing here rewrites history, pushes, or reads the key password.
Tests: `tests/test_apk_check.py`.
