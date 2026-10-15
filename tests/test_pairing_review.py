from __future__ import annotations

from datetime import datetime, timedelta

from app.remote import pairing
from app.remote.pairing import PairingStore


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now().timestamp()

    def __call__(self) -> float:
        return self.now


def paired(store: PairingStore) -> tuple[str, str]:
    return store.pair(store.new_code(), "Phone")


def test_a_code_works_once_and_a_wrong_key_never() -> None:
    store = PairingStore(None, clock=Clock(), encrypt=False)
    code = store.new_code()
    first = store.pair(code, "A")
    assert first is not None and store.pair(code, "B") is None
    device_id, key = first
    assert store.check(device_id, key) and store.check(device_id, key + "x") is None


def test_a_phone_idle_for_months_has_to_pair_again() -> None:
    clock = Clock()
    store = PairingStore(None, clock=clock, encrypt=False)
    device_id, key = paired(store)
    assert store.check(device_id, key)
    clock.now += (pairing.IDLE_DAYS - 1) * 86400
    assert store.check(device_id, key)
    clock.now += 2 * 86400
    assert store.check(device_id, key) is None and device_id in store.devices  # still listed, so it can be removed


def test_using_the_phone_keeps_it_alive() -> None:
    clock = Clock()
    store = PairingStore(None, clock=clock, encrypt=False)
    device_id, key = paired(store)
    clock.now += 60 * 86400
    device = store.check(device_id, key)
    device.last_seen = datetime.fromtimestamp(clock.now).astimezone().isoformat(timespec="minutes")
    clock.now += 60 * 86400
    assert store.check(device_id, key)


def test_a_damaged_date_expires_the_phone() -> None:
    store = PairingStore(None, clock=Clock(), encrypt=False)
    device_id, key = paired(store)
    store.devices[device_id].paired_at = "garbage"
    assert store.check(device_id, key) is None


def test_five_wrong_codes_lock_pairing_and_it_unlocks() -> None:
    clock = Clock()
    store = PairingStore(None, clock=clock, encrypt=False)
    code = store.new_code()
    for _ in range(5):
        assert store.pair("000000" if code != "000000" else "111111", "x") is None
    assert store.locked() and store.pair(code, "A") is None  # even the right code is refused while locked
    clock.now += pairing.LOCK_SECONDS + 1
    assert not store.locked()
    assert store.pair(store.new_code(), "A") is not None
    assert timedelta(seconds=pairing.CODE_SECONDS) <= timedelta(minutes=5)
