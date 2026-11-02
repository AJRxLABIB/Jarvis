# Digital twin and what-if preview (round 52)

**Twin** (`app/core/twin.py`, table `twin_observations`, tool `digital_twin`, Intelligence > Digital twin): the last thing Jarvis
*observed* about devices, missions, free disk space, memory and paths it checked - each with its source and time. Observations are
made only on request (`refresh`) or during a what-if check, from stores Jarvis already keeps and cheap local reads. Nothing is watched
in the background. An entry older than its lifetime (devices 5 min, missions 2 min, disk 10 min, memory 2 min, paths 10 min) is
shown **STALE**; things never observed are **UNKNOWN**, not "fine".

**What-if** (`digital_twin` with `steps` or `mission_id`): without running anything, per step: validated arguments, risk level, paths
touched (resolved the way the tools resolve them), missing paths (unless an earlier step creates them), name collisions, symbolic
links, whether Jarvis has an automatic undo (`undo.py` set), Recycle-Bin-only or irreversible, internet need versus offline mode,
plausible failure modes, a safer alternative where one exists (e.g. preview a batch rename first), and stale devices the plan names.

Limits: an **estimate** from tool metadata and a few reads, not a model of the application and not a guarantee. It does not predict
what an external program will do. Nothing is executed and nothing outside the twin's own table is written.
Tests: `tests/test_twin.py`; lab suite "digital twin".
