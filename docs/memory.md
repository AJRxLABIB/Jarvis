# Memory 2.0 (3.17)

`app/memory/manager.py` (store) + `app/memory/memory2.py` (types and contradiction rules). Migration 17 adds the
columns and two tables; nothing existing is changed or lost.

| Type | Meaning | Example |
|---|---|---|
| semantic | a fact or preference | "my editor is VS Code", "TMS uses PostgreSQL" |
| episodic | something that happened | "I finished the TMS login yesterday"; finished missions (recorded by Jarvis) |
| procedural | how you like things done | "when I start work, open VS Code and Spotify" |
| goal | what you're working towards | "my goal is to learn Rust this year" |
| working | the current conversation + the context engine snapshot | never stored |

Every memory has: id, type, content, source (provenance), created/updated time, **stated or recorded** (you said
it vs Jarvis saw it happen), confidence (1.0 you said/typed it, 0.9 you accepted a suggestion, 0.8 recorded),
importance 1-3 ("remember this, it's important" = 3; important memories go to the model first), privacy,
expiry, tags, links to other memories, and its edit history.

## Contradictions

Deterministic rules, not model guesses. Two memories that name different things of the same kind (databases,
editors, browsers, frameworks, operating systems, clouds, phones) about the same subject:

* the new one says it replaced the old ("migrated to", "switched from X to Y", "no longer use X", "instead of X")
  -> the old memory is **superseded**: kept in the Memory window as history, never given to the model again;
* otherwise -> a **conflict** is opened: "That may contradict 'TMS uses MongoDB'. Which is true now?" Say
  "keep the new one", "keep the old one" or "both are true" (or "any memory conflicts?").

Different subjects don't conflict ("my blog uses MySQL" and "TMS uses MongoDB" are both kept).

## Why do you remember this?

"Why do you remember that?" gives: where it came from, when, by voice/typed/phone, the type, whether you said it
or Jarvis recorded it, confidence, importance, earlier versions, what it replaced or what replaced it, open
conflicts, privacy and expiry. Recorded memories are never presented as something you said.
