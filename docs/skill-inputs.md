# Reusable skills with named inputs (round 50)

Example: you open Chrome, then a different site each day, then set the volume. The Experience Compiler (`experience propose`)
now proposes one routine "open_application, open_website, volume_control" with an input `site` (default = the latest site, description =
real examples). `experience save` stores it; run it for any site:

    run_workflow name="Open a site" params={"site": "github.com"}

## Rules (tested)
* Declared inputs: at most 5, names `[a-z][a-z0-9_]{0,29}`, text/number defaults only.
* Values: text or number, at most 300 characters, no control characters, no `{{` (no template injection). Unknown inputs and
  missing inputs without a default are refused before any step runs.
* Only argument values are substituted. A tool name, a step type, `when`, a permission or an approval can never come from an input, and
  every step still runs through the normal executor (permission levels, path policy, confirmation, emergency stop).
* A step may use `{{name}}` only if declared, and a declared input must be used (checked at save time).
* The compiler only makes inputs from text that differed between runs; numbers/lists stay fixed and are reported; anything
  that looks secret is never an input. Saving still asks you on screen.
* Storage: table `workflow_parameters` (migration 40). Replacing a workflow keeps the inputs if its steps still use them;
  `restore_workflow` of an old version keeps them only when they still match.

## Limits
No UI editor for inputs yet; no typed inputs (choice/number/path validation beyond the tool's own); not exercised on real applications.
