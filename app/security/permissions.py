"""Permission levels and the decision whether a tool call may run.

Level 0 SAFE       - runs automatically (time, system info, opening apps)
Level 1 MODERATE   - asks first when ``confirm_moderate`` is on (moving/renaming files)
Level 2 DANGEROUS  - always asks (delete, shutdown, kill process)
Level 3 CRITICAL   - refused while ``block_critical`` is on (format disk, disable security)

Only this module decides. The language model cannot mark an action as confirmed
or lower its level; config overrides can only RAISE a tool's level.
"""

from __future__ import annotations

from enum import Enum, IntEnum

from app.config.files import PermissionsConfig


class RiskLevel(IntEnum):
    SAFE = 0
    MODERATE = 1
    DANGEROUS = 2
    CRITICAL = 3


class Decision(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    BLOCK = "block"


class PermissionEngine:
    def __init__(self, config: PermissionsConfig) -> None:
        self.config = config

    def effective_level(self, tool_name: str, level: RiskLevel | int) -> RiskLevel:
        override = self.config.tool_levels.get(tool_name)
        base = RiskLevel(int(level))
        return RiskLevel(max(base, override)) if override is not None else base

    def decide(self, tool_name: str, level: RiskLevel | int) -> tuple[Decision, RiskLevel]:
        effective = self.effective_level(tool_name, level)
        if effective == RiskLevel.SAFE:
            return Decision.ALLOW, effective
        if effective == RiskLevel.MODERATE:
            return (Decision.CONFIRM if self.config.confirm_moderate else Decision.ALLOW), effective
        if effective == RiskLevel.DANGEROUS:
            return Decision.CONFIRM, effective
        return (Decision.BLOCK if self.config.block_critical else Decision.CONFIRM), effective
