"""Builds the tool registry.

To add a tool module: create ``app/tools/<name>.py`` with a ``TOOLS`` tuple and
add it to ``TOOL_MODULES`` below.
"""

from __future__ import annotations

from app.agent.tool_manager import ToolRegistry
from app.tools import (
    activity, applications, assistant, automation, basic, browser, developer, documents, filesystem, focus, housekeeping, knowledge,
    memory, outlook, phone, productivity, screen_agent, smart_home, system, vision, voice_control, windows, windows_settings,
    code_tools, device_tools, device_toggles, evolution_tools, research, session_tools, context_tools, proactive_tools, file_tools, goals_missions, ui_elements, layouts, messages, undo_tools, youtube,
    agents_tools, diagnostics_tools, github, profiles_tool, google, routines_tool, history_tool, clipboard_tool, approvals_tool,
    file_batch, finance_tool, phone_link_tool, personal_tools, local_home_tool, growth_tools,
)

TOOL_MODULES = (basic, assistant, applications, filesystem, documents, productivity, focus, housekeeping, knowledge, outlook,
                system, windows, windows_settings,
                phone, screen_agent, smart_home, developer, memory, activity, browser, automation, vision, voice_control,
                youtube, layouts, messages, undo_tools, goals_missions, device_toggles, file_tools, ui_elements, code_tools, proactive_tools, device_tools,
                evolution_tools, research, session_tools, context_tools, agents_tools, diagnostics_tools, github, profiles_tool, google, routines_tool, history_tool, clipboard_tool, approvals_tool,
                file_batch, finance_tool, phone_link_tool, personal_tools, local_home_tool, growth_tools)


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for module in TOOL_MODULES:
        for tool in module.TOOLS:
            registry.register(tool)
    return registry
