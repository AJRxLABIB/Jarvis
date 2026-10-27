"""What an approval must show BEFORE you say yes: the actual action, the target, the risk in words, and what it can
affect. Built only from the request itself (never from model prose about what it "will" do)."""

from __future__ import annotations

from dataclasses import dataclass

from app.ui.command_center.timeline import target_of

RISK_WORDS = {
    "safe": ("Low risk", "Reads or opens something; nothing is changed."),
    "moderate": ("Changes something", "It changes a setting, file or app, usually in a way you can undo."),
    "dangerous": ("Sensitive", "It can affect other people, money, data or the PC itself, and may not be undoable."),
    "critical": ("Critical", "It is normally refused; it only runs because you allow it here, on screen."),
}


@dataclass(frozen=True)
class ApprovalView:
    action: str
    target: str
    risk: str
    risk_note: str
    tool: str
    details: tuple[tuple[str, str], ...]

    def lines(self) -> list[str]:
        out = [f"Action: {self.action}", f"Target: {self.target}", f"Risk: {self.risk} - {self.risk_note}"]
        out += [f"{k}: {v}" for k, v in self.details]
        return out


def describe(request) -> ApprovalView:
    details = request.details if isinstance(request.details, dict) else {}
    arguments = details.get("arguments") if isinstance(details.get("arguments"), dict) else details
    risk, note = RISK_WORDS.get(request.level_name, ("Unknown", ""))
    shown = tuple((str(k).replace("_", " "), str(v)[:160]) for k, v in list(arguments.items())[:6]
                  if k != "arguments" and v not in (None, "", [], {}))
    return ApprovalView(request.summary, target_of(request.tool, arguments), risk, note, request.tool, shown)
