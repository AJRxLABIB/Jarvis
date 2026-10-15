"""The evolution engine: Jarvis improving itself, safely.

    analyze -> proposal -> (explain, diff, risk) -> test on a COPY -> report -> your approval -> backup ->
    apply -> verify -> (roll back any time)

Rules that are enforced in code, not left to the model:

* The model can only PROPOSE a change (exact find/replace edits). Proposing never touches the source.
* The security core can't be changed this way at all (PROTECTED): permissions, the executor, taint, emergency
  controls, the evolution engine itself and the security tests.
* A proposal is tested on a copy of the source with the project's own test suite and linter. It passes only if
  nothing fails, no test was removed, and the linter is clean.
* Applying needs a tested proposal, your confirmation (DANGEROUS: asked every time, also for your own voice
  command), and a source that hasn't changed since the test. The originals are backed up first and every
  written file is checked afterwards.
* Rolling back restores the backup - only if the files are still exactly what was applied.
* There is no "apply automatically" policy. Update policy for code: manual / ask only.
"""
