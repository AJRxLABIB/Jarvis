# Agents and the orchestrator

Code: `app/core/agents.py` (contracts), `app/core/orchestrator.py` (running a plan), `app/tools/agents_tools.py`
(the `list_agents`, `delegate`, `run_team` tools). Enforcement lives in `app/agent/tool_manager.py`.

## What an agent is

An agent is **not** a separate AI with its own powers. It is a **contract**: a name, the tool **categories** it
may use, and the highest **risk** it may reach. When a step runs "as" an agent, the executor refuses any tool
outside that contract, deterministically, before the tool runs. The model may propose which agent does what; it
can never widen what an agent is allowed to do. This keeps the master rule: the LLM is an untrusted planner;
permissions, risk, confirmation and audit stay with the kernel.

A contract only ever **narrows** what is already allowed. It is applied on top of the permission engine, the taint
firewall, confirmations and the audit log - never instead of them. The **strictest** of (contract, permissions)
wins.

## The built-in team

| Agent | Can use | Ceiling | Never |
|---|---|---|---|
| researcher | web, browser, memory, info | safe (read-only) | no memory writes/erase |
| planner | memory, automation, info, basic | safe (read-only) | no starting missions/automations |
| organizer | files | moderate | delete_item, empty_recycle_bin |
| coder | dev, files | moderate | run_command, run_project, git_commit, deletes |
| operator | apps, windows, info, screen, clipboard | moderate | close_all_apps, power_control, windows_update |
| secretary | office, phone, info, memory | safe | send_email, text_contact, whatsapp_message, call_contact |

Read-only agents (researcher, planner, secretary) cap at **safe**, so nothing that changes the PC can run under
them even if the plan asks. Acting agents cap at **moderate**, so dangerous/critical steps (deletes, force-kill,
shutdown, running commands, self-update) are refused inside a delegated task and must be done by you directly.

## Delegating

- **`list_agents`** - the team and what each may do ("list agents", "who's on my team").
- **`delegate`** - give one agent a short list of tool calls to do now.
- **`run_team`** - split a job across several agents; each step names an agent and a tool call.

Both validate the whole plan first (every step's tool must sit inside its agent's contract) and ask once before
anything changes. At run time each step goes through the executor's full checks with the agent contract active.
Orchestration does not nest: `delegate` and `run_team` can't be steps inside a mission or inside each other.

This is a here-and-now delegation, not a restart-surviving job - for that, use a mission
([docs/missions.md](docs/missions.md)); missions and agents share the same executor and safety kernel.
