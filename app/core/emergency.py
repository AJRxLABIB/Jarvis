"""Emergency controls: stop everything, and switches that only a human at the PC can turn back on.

* ``emergency stop`` (voice, Menu, or Ctrl+Shift+F12 anywhere): the current request is cancelled, speech stops,
  a running mission pauses, the song queue is cleared, a restart/shutdown countdown is cancelled, and
  automations are switched off.
* Switches: disable automations, disable the phone remote, disable network actions (anything that goes online
  or opens websites), lock Jarvis (only read-only tools run).

**Turning something OFF works by voice. Turning it back ON only works in the window** (Menu -> Emergency
controls): a voice - yours from a speaker, a video, someone else - must not be able to undo an emergency stop.
The switches survive a restart (settings store). Every change is an EMERGENCY event (audit + notification).
"""

from __future__ import annotations

from dataclasses import dataclass, field

SWITCHES = ("automation_disabled", "remote_disabled", "network_disabled", "locked", "evolution_disabled")
LABELS = {"automation_disabled": "automations off", "remote_disabled": "phone remote off",
          "network_disabled": "network actions off", "locked": "Jarvis locked (read-only)",
          "evolution_disabled": "self-improvement off (no testing or applying changes)"}
KEY = "emergency_switches"


@dataclass
class Emergency:
    store: object = None
    flags: dict[str, bool] = field(default_factory=lambda: {s: False for s in SWITCHES})

    def load(self, store) -> None:
        self.store = store
        saved = store.get(KEY) if store is not None else None
        if isinstance(saved, dict):
            self.flags.update({k: bool(v) for k, v in saved.items() if k in SWITCHES})

    def set(self, switch: str, on: bool, *, by_voice: bool) -> bool:
        """Returns False when refused (turning a switch back OFF by voice)."""
        if switch not in SWITCHES:
            raise ValueError(switch)
        if not on and by_voice:
            return False
        self.flags[switch] = on
        if self.store is not None:
            self.store.set(KEY, dict(self.flags))
        from app.core import events

        try:
            events.BUS.publish(events.EMERGENCY, message=f"{LABELS[switch]}: {'on' if on else 'off'}",
                               switch=switch, on=on)
        except Exception:
            pass
        return True

    def active(self) -> list[str]:
        return [LABELS[s] for s, on in self.flags.items() if on]

    def blocks(self, *, level: int, needs_network: bool, egress: bool) -> str | None:
        """Why the executor must refuse this call now (None = allowed)."""
        if self.flags["locked"] and level > 0:
            return "Jarvis is locked (emergency controls): only read-only actions run. Unlock it in the window."
        if self.flags["network_disabled"] and (needs_network or egress):
            return "Network actions are switched off (emergency controls). Turn them back on in the window."
        return None


STATE = Emergency()
