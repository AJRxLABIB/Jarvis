"""The command-center view: a left rail, a conversation + command area, a context panel and a compact status area.
Pure view-models (states, timeline, devices, security, automations) live beside the Qt widgets so they can be tested
without a screen. Nothing here invents data: a capability or metric that isn't known is shown as unknown."""
