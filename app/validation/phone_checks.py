"""Phone validation: real actions on your real paired phone, with the result confirmed by you.

``python run.py --validate phone`` talks to the phone through the same signed channel as everyday use (``PhoneLink``): it checks that a
trusted phone exists and has checked in recently, that its app is the version this PC expects and allows control, then sends harmless
real actions (show a message, flashlight on / off), measures how long the phone took to carry each out, and ASKS YOU whether the effect
really happened - the phone's own "done" is a claim, your eyes are the evidence. ``--full`` adds ringing the phone and a reconnect test
(turn Wi-Fi off on the phone, then back on, and watch how long it takes to check in again).

Nothing risky is ever sent here (no calls, no SMS, no lock, no screen reading). The decisions are tested with a fake phone; whether YOUR
phone passes is only known when you run it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.validation.report import FAIL, PASS, PENDING, SKIP, WARN, Report, Result, timed

FRESH_SECONDS = 300
STALE_SECONDS = 3600
SLOW_ACTION_SECONDS = 15.0


@dataclass
class Outcome:
    status: str                 # done | failed | expired | unsupported | pending | cancelled
    result: str = ""
    seconds: float = 0.0


class PhoneEnv:
    """What the checks need. ``RealPhoneEnv`` uses the real registry, queue and your answers; tests use a fake."""

    def phones(self) -> list[dict[str, Any]]: ...                       # trusted, not revoked: {"id", "name"}
    def state(self, device_id: str) -> dict[str, Any]: ...               # {"seen": iso, "battery", "control", "comms", "app_version", ...}
    def now(self) -> datetime: ...
    def act(self, device_id: str, action: str, args: dict[str, Any], seconds: float) -> Outcome: ...
    def confirm(self, question: str) -> bool | None: ...                 # your yes / no (None = no answer)
    def expected_version(self) -> str: ...
    def wait_checkin(self, device_id: str, since_iso: str, timeout: float) -> float | None: ...   # seconds until the next check-in


def _age_seconds(env: PhoneEnv, state: dict[str, Any]) -> float | None:
    try:
        return (env.now() - datetime.fromisoformat(state["seen"]).astimezone(timezone.utc)).total_seconds()
    except (KeyError, ValueError, TypeError):
        return None


def check_paired(env: PhoneEnv) -> tuple[str, str]:
    phones = env.phones()
    if not phones:
        return PENDING, "no trusted phone is paired (Menu > Mobile devices > Pair a new phone), so nothing about the phone can be validated"
    return PASS, f"{len(phones)} trusted phone(s): " + ", ".join(p["name"] for p in phones)


def check_checkin(env: PhoneEnv, phone: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    state = env.state(phone["id"])
    if not state:
        return FAIL, f"{phone['name']} has never checked in: open JARVIS Mobile on it", {}
    age = _age_seconds(env, state)
    ev = {"age_seconds": age, "battery": state.get("battery")}
    if age is None:
        return WARN, "the phone's last check-in time could not be read", ev
    if age <= FRESH_SECONDS:
        return PASS, f"checked in {int(age)} s ago", ev
    if age <= STALE_SECONDS:
        return WARN, f"last check-in {int(age // 60)} min ago: the app is closed or background checks are off (actions wait until it checks in)", ev
    return FAIL, f"last check-in {age / 3600:.1f} h ago: the phone is not reaching this PC (same Wi-Fi? app open? 'Stay connected' on?)", ev


def check_app(env: PhoneEnv, phone: dict[str, Any]) -> tuple[str, str]:
    state = env.state(phone["id"])
    version, wanted = state.get("app_version"), env.expected_version()
    if not version:
        return WARN, "the phone did not report its app version (an old JARVIS Mobile): rebuild and install the current one"
    if version != wanted:
        return WARN, f"the phone runs JARVIS Mobile {version}, this PC expects {wanted}: newer actions may be 'unsupported'"
    return PASS, f"JARVIS Mobile {version} matches this PC"


def check_control_switch(env: PhoneEnv, phone: dict[str, Any]) -> tuple[str, str]:
    if env.state(phone["id"]).get("control"):
        return PASS, "the phone allows control from this PC"
    return FAIL, "control from the PC is OFF on the phone (JARVIS Mobile > Settings > 'Let my PC control this phone'): the PC cannot turn it on"


def check_action(env: PhoneEnv, phone: dict[str, Any], action: str, args: dict[str, Any], question: str) -> tuple[str, str, dict[str, Any]]:
    outcome = env.act(phone["id"], action, args, 20.0)
    ev = {"status": outcome.status, "seconds": round(outcome.seconds, 1), "reported": outcome.result[:120]}
    if outcome.status == "unsupported":
        return WARN, f"'{action}': the phone app is too old for this action", ev
    if outcome.status in ("failed", "expired", "cancelled"):
        return FAIL, f"'{action}' {outcome.status}" + (f": {outcome.result[:120]}" if outcome.result else "") + f" after {outcome.seconds:.1f} s", ev
    if outcome.status != "done":
        return FAIL, f"'{action}' was not carried out within {outcome.seconds:.0f} s (the phone did not collect it - app closed?)", ev
    seen = env.confirm(question)
    ev["you_confirmed"] = seen
    if seen is None:
        return PENDING, f"'{action}': the phone said it was done in {outcome.seconds:.1f} s, but you did not confirm the effect", ev
    if not seen:
        return FAIL, f"'{action}': the phone reported success in {outcome.seconds:.1f} s but YOU did not see the effect - a false success report", ev
    if outcome.seconds > SLOW_ACTION_SECONDS:
        return WARN, f"'{action}' worked and you confirmed it, but it took {outcome.seconds:.1f} s", ev
    return PASS, f"'{action}' worked in {outcome.seconds:.1f} s and you confirmed it", ev


def check_reconnect(env: PhoneEnv, phone: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    since = env.now().isoformat(timespec="seconds")
    seconds = env.wait_checkin(phone["id"], since, 120.0)
    if seconds is None:
        return FAIL, "the phone did not check in again within 2 minutes of coming back online", {}
    if seconds <= 30:
        return PASS, f"the phone checked in {seconds:.0f} s after reconnecting", {"seconds": seconds}
    return WARN, f"the phone needed {seconds:.0f} s to check in after reconnecting (Android may be limiting background work: allow JARVIS Mobile to run in the background)", {"seconds": seconds}


def run(env: PhoneEnv, *, full: bool = False) -> Report:
    report = Report("phone")
    first = timed("a trusted phone is paired", lambda: check_paired(env))
    report.add(first)
    if first.status == PENDING:
        return report
    phone = env.phones()[0]
    report.environment = {"phone": phone["name"]}
    report.add(timed("recent check-in", lambda: check_checkin(env, phone)))
    report.add(timed("app version", lambda: check_app(env, phone)))
    control = report.add(timed("control from the PC is allowed", lambda: check_control_switch(env, phone)))
    if control.status == FAIL:
        for name in ("show a message", "flashlight on", "flashlight off"):
            report.add(Result(name, SKIP, "control is off on the phone"))
        return report
    report.add(timed("show a message on the phone", lambda: check_action(
        env, phone, "show_text", {"text": "JARVIS validation: if you can read this, the message arrived."}, "Did the phone show the message 'JARVIS validation'?")))
    report.add(timed("flashlight on", lambda: check_action(env, phone, "flashlight_on", {}, "Is the phone's flashlight ON now?")))
    report.add(timed("flashlight off", lambda: check_action(env, phone, "flashlight_off", {}, "Is the phone's flashlight OFF now?")))
    if full:
        report.add(timed("ring the phone (3 seconds)", lambda: check_action(env, phone, "ring", {"seconds": 3}, "Did the phone ring (a loud tone) for a few seconds?")))
        report.add(timed("reconnect after losing the network", lambda: check_reconnect(env, phone)))
    else:
        report.add(Result("ring the phone", PENDING, "add --full to ring it and test reconnecting"))
        report.add(Result("reconnect after losing the network", PENDING, "add --full (you will turn Wi-Fi off and on on the phone)"))
    # things only a person with the phone can judge are listed, never auto-passed
    for item in MANUAL_CHECKLIST:
        report.add(Result(item, PENDING, "manual: do this on the phone and note the result in docs/phone-validation.md"))
    return report


MANUAL_CHECKLIST = (
    "Android lets JARVIS Mobile keep checking in with the screen off for 30 minutes (battery optimisation off)",
    "pairing: revoking the phone on the PC makes its next command fail, and re-pairing works",
    "notifications forwarded from the phone arrive on the PC and codes are hidden",
    "sending a file from the phone (Share > JARVIS Mobile) lands in the PC's phone folder",
    "the app's own emergency stop works from the phone while a PC action is running",
)


class RealPhoneEnv(PhoneEnv):
    """Real registry and queue; your answers come from the keyboard. Run by you; not exercised by the automated tests."""

    def __init__(self, ctx: Any) -> None:
        from app.remote.mobile import MobileRegistry
        from app.remote.phone_link import PhoneLink

        self.registry, self.link = MobileRegistry(ctx.database), PhoneLink(ctx.database)

    def phones(self) -> list[dict[str, Any]]:
        out = [{"id": d.id, "name": d.name} for d in self.registry.all() if d.trust == "trusted" and not d.revoked_at]
        out.sort(key=lambda p: self.link.state(p["id"]).get("seen", ""), reverse=True)
        return out

    def state(self, device_id: str) -> dict[str, Any]:
        return self.link.state(device_id)

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def act(self, device_id: str, action: str, args: dict[str, Any], seconds: float) -> Outcome:
        started = time.monotonic()
        command = self.link.queue(device_id, action, args)
        outcome = self.link.wait(command, seconds)
        return Outcome(outcome["status"], outcome.get("result") or "", time.monotonic() - started)

    def confirm(self, question: str) -> bool | None:
        try:
            answer = input(f"   {question} [y/n] ").strip().lower()
        except EOFError:
            return None
        return True if answer.startswith("y") else False if answer.startswith("n") else None

    def expected_version(self) -> str:
        from app.remote.apk import MOBILE_APP_VERSION

        return MOBILE_APP_VERSION

    def wait_checkin(self, device_id: str, since_iso: str, timeout: float) -> float | None:
        print("   On the phone: turn Wi-Fi (and mobile data) OFF. I will notice when it stops checking in...", flush=True)
        began = time.monotonic()
        while time.monotonic() - began < timeout:
            seen = self.state(device_id).get("seen", "")
            age = (self.now() - datetime.fromisoformat(seen)).total_seconds() if seen else 1e9
            if age > 20:
                break
            time.sleep(1.0)
        else:
            return None                      # it never went quiet: the network was not really switched off
        try:
            input("   The phone has stopped checking in. Turn Wi-Fi back ON, then press Enter here... ")
        except EOFError:
            return None
        baseline, t0 = self.state(device_id).get("seen", ""), time.monotonic()
        while time.monotonic() - t0 < timeout:
            seen = self.state(device_id).get("seen", "")
            if seen and seen > baseline:
                return time.monotonic() - t0
            time.sleep(0.5)
        return None
