# Personal knowledge graph (3.18)

`app/core/knowledge_graph.py`. Built on demand from what Jarvis already has - nothing extra is collected or stored.

| Node | From |
|---|---|
| project | projects you told Jarvis about (DEV_PROJECT_DIR, "my project folder is...", trusted projects) |
| technology | the project's files (stack markers, package.json, requirements.txt, composer.json, go.mod) and technologies named in memories |
| file | entry points, and files changed in the last 14 days (`git log --since=14.days --name-only`, fixed arguments) |
| memory / decision | Memory 2.0 (a memory with decision words - decided, chose, switched, migrated, because, we use... - or one that was replaced, is a decision); private and switched-off memories are left out |
| goal, mission | goals and Mission Control |
| request / action | your requests (agent runs) and Jarvis's actions (audit log), for "what did I work on" |

Links: project *uses* technology, project *contains* file, memory/decision/goal/mission *about* project or
technology, newer memory *replaced* older memory.

## Questions ("ask_knowledge", Menu -> Knowledge, or just ask)

* "which files are related to TMS" / "which files does TMS have"
* "what decision did I make about the database" / "why did we choose PostgreSQL"
* "which project uses Laravel" / "which project contains AuthController"
* "what was I working on last week / yesterday / this month / in the last 3 days"
* "everything related to TMS"

Answers list their evidence (memory #, git log, project files, your requests, Jarvis's actions). "What was I
working on" only knows work done through Jarvis - it says so instead of guessing about work done by hand.
