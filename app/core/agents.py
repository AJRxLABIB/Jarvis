"""Agent contracts: named, role-scoped limits that a task runs under, enforced by the SAME executor as
everything else (app/agent/tool_manager.py).

Why this exists. The orchestrator (app/core/orchestrator.py) lets Jarvis split a big job across specialist
"agents" - a researcher, a file organizer, a coder, and so on. An agent is NOT a separate AI with its own
powers: it is a *contract* that says which tool CATEGORIES it may use and the highest RISK it may reach. When a
step runs "as" an agent, the executor refuses any tool outside that contract, deterministically, before the tool
runs - the model's plan can never widen what an agent is allowed to do. This keeps the master rule: the LLM is an
untrusted planner; permissions, risk and confirmation stay with the kernel.

A contract only ever NARROWS what is already allowed. It is applied on top of the normal permission engine,
taint firewall, confirmations and audit - never instead of them. The strictest of (contract, permissions) wins.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.security.permissions import RiskLevel

# Tool categories in the registry (tool.category). Kept here so a typo in a contract is caught by the test that
# checks every listed category exists.
KNOWN_CATEGORIES = frozenset({
    "apps", "automation", "basic", "browser", "clipboard", "dev", "evolution", "files", "home", "info", "memory",
    "notes", "office", "phone", "screen", "system", "todo", "vision", "voice", "web", "windows",
})


@dataclass(frozen=True)
class AgentContract:
    name: str
    description: str
    categories: frozenset[str]        # tool categories this agent may use
    max_risk: RiskLevel = RiskLevel.MODERATE  # the highest risk it may reach (DANGEROUS/CRITICAL refused)
    deny_tools: frozenset[str] = field(default_factory=frozenset)  # specific tools it may never use
    allow_tools: frozenset[str] = field(default_factory=frozenset)  # specific extra tools (still within max_risk)
    max_steps: int = 6                # most steps one plan may give this agent
    max_seconds: float = 300.0        # most total running time one plan may spend on this agent's steps
    max_tokens: int = 6000            # estimated tokens (4 characters each) of results this agent may hand back in one plan

    def allows(self, tool_name: str, category: str, level: RiskLevel) -> tuple[bool, str]:
        """(may this agent run this tool at this risk, why not). Deterministic - no model involved."""
        if tool_name in self.deny_tools:
            return False, f"the {self.name} agent is not allowed to use {tool_name}"
        if level > self.max_risk:
            return False, (f"the {self.name} agent may only do up to {self.max_risk.name.lower()} actions, and "
                           f"{tool_name} is {level.name.lower()}")
        if tool_name in self.allow_tools or category in self.categories:
            return True, ""
        return False, f"the {self.name} agent only handles {', '.join(sorted(self.categories))} tasks, not {tool_name}"

    def summary(self) -> str:
        return (f"{self.name}: {self.description} (can use {', '.join(sorted(self.categories))}; "
                f"up to {self.max_risk.name.lower()})")


# Built-in agents. Read-only agents have max_risk SAFE, so nothing that changes the PC can run under them even if
# the plan asks for it. Agents that act cap out at MODERATE, so DANGEROUS/CRITICAL steps (deletes, force-kill,
# shutdown, code execution, self-update) are refused inside a delegated task and must be done directly by you.
AGENTS: dict[str, AgentContract] = {
    "researcher": AgentContract(
        "researcher", "finds things out on the web and in your own knowledge, and reports back",
        frozenset({"web", "browser", "memory", "info"}), RiskLevel.SAFE,
        deny_tools=frozenset({"forget", "forget_everything", "forget_history", "remember", "manage_memory"})),
    "planner": AgentContract(
        "planner", "reads your context, goals and notes and proposes a plan - never acts",
        frozenset({"memory", "automation", "info", "basic"}), RiskLevel.SAFE,
        deny_tools=frozenset({"forget", "forget_everything", "forget_history", "manage_memory",
                              "start_mission", "resume_mission", "run_workflow", "manage_automation"})),
    "organizer": AgentContract(
        "organizer", "tidies and converts files (never deletes)",
        frozenset({"files"}), RiskLevel.MODERATE,
        deny_tools=frozenset({"delete_item", "empty_recycle_bin"})),
    "coder": AgentContract(
        "coder", "reads, searches and edits code in your projects (never runs commands)",
        frozenset({"dev", "files"}), RiskLevel.MODERATE,
        deny_tools=frozenset({"run_command", "run_project", "git_commit", "delete_item", "empty_recycle_bin"})),
    "operator": AgentContract(
        "operator", "drives the PC: apps, windows, volume, brightness, media",
        frozenset({"apps", "windows", "info", "screen", "clipboard"}), RiskLevel.MODERATE,
        deny_tools=frozenset({"close_all_apps", "power_control", "windows_update", "screen_recording"})),
    "secretary": AgentContract(
        "secretary", "reads your calendar, email and messages and drafts replies (never sends on its own)",
        frozenset({"office", "phone", "info", "memory"}), RiskLevel.SAFE,
        deny_tools=frozenset({"send_email", "text_contact", "whatsapp_message", "call_contact"})),
}

AGENTS["reviewer"] = AgentContract(
    "reviewer", "reads code and changes and reports problems - never edits",
    frozenset({"dev", "files"}), RiskLevel.SAFE, max_steps=8,
    deny_tools=frozenset({"run_command", "run_project", "git_commit", "delete_item", "empty_recycle_bin"}))
AGENTS["verifier"] = AgentContract(
    "verifier", "re-checks another agent's result from the real state of the PC (files, screen, app) - never changes anything",
    frozenset({"files", "screen", "windows", "info", "dev"}), RiskLevel.SAFE, max_steps=6, max_seconds=120.0,
    deny_tools=frozenset({"run_command", "run_project", "git_commit"}))
AGENTS["analyst"] = AgentContract(
    "analyst", "reads your documents, notes, memories and files and summarises or compares them - never changes anything",
    frozenset({"memory", "files", "notes", "info"}), RiskLevel.SAFE, max_steps=8,
    deny_tools=frozenset({"forget", "forget_everything", "forget_history", "remember", "manage_memory"}))

DEFAULT_AGENT = "researcher"


def estimate_tokens(text: str) -> int:
    """A rough count (4 characters per token). Jarvis agents don't run their own model; this limits how much
    tool output one agent can pour back into the conversation, which is what costs the real model's tokens."""
    return (len(text) + 3) // 4


def contract_for(name: str) -> AgentContract | None:
    return AGENTS.get(str(name or "").strip().lower())


def names() -> list[str]:
    return list(AGENTS)
