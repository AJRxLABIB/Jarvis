"""mDNS discovery: wire format, safety against hostile packets, and 'found is never trusted'."""

from __future__ import annotations

import socket
import struct

import pytest

from tests.test_v3210 import db  # noqa: F401  (fixture)

from app.remote import discovery as D

MINE = D.Announcement("Jarvis-ab12cd34", "labib-pc.local.", "192.168.1.20", 8765, (("v", "1"), ("id", "ab12cd34ef56"), ("name", "Labib PC")))


def test_query_is_answered_with_all_four_records():
    reply = D.answer_for(D.build_query(), MINE)
    assert reply is not None
    is_response, questions, records = D.parse_message(reply)
    assert is_response and questions == []
    kinds = {r.rtype: r for r in records}
    assert kinds[D.TYPE_PTR].value == MINE.full_name
    assert kinds[D.TYPE_SRV].value == (8765, "labib-pc.local.")
    assert kinds[D.TYPE_TXT].value == {"v": "1", "id": "ab12cd34ef56", "name": "Labib PC"}
    assert kinds[D.TYPE_A].value == "192.168.1.20"


def test_unrelated_questions_and_responses_get_no_answer():
    assert D.answer_for(D.build_query("_http._tcp.local."), MINE) is None
    assert D.answer_for(D.build_response(MINE), MINE) is None          # never answer an answer (no feedback loop)
    assert D.answer_for(b"junk", MINE) is None


def test_hostile_packets_raise_or_are_skipped_never_crash():
    good = D.build_response(MINE)
    for bad in (b"", b"\x00" * 11, good[:20], good[:-3],
                struct.pack("!HHHHHH", 0, 0x8400, 0, 60000, 0, 0), b"\xff" * 5000, bytes(10000)):
        with pytest.raises(D.DnsError):
            D.parse_message(bad)
    assert D.collect([(good[:-3], "10.0.0.1"), (b"\xff" * 100, "10.0.0.2")]) == []


def test_pointer_loop_is_cut():
    loop = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0) + b"\xc0\x0c" + struct.pack("!HH", 12, 1)
    with pytest.raises(D.DnsError):
        D.parse_message(loop)


def test_collect_uses_the_real_sender_not_the_claimed_address_and_skips_self():
    spoof = D.Announcement("Jarvis-evil", "evil.local.", "10.9.9.9", 8765, (("id", "ffffffffffff"), ("name", "Totally Safe")))
    found = D.collect([(D.build_response(MINE), "192.168.1.20"), (D.build_response(spoof), "192.168.1.77")], own_id="")
    by = {f.instance: f for f in found}
    assert by["Jarvis-evil"].address == "192.168.1.77"                    # not 10.9.9.9
    assert D.collect([(D.build_response(MINE), "192.168.1.20")], own_id="ab12cd34ef56") == []


def test_text_from_the_network_is_cleaned():
    nasty = D.Announcement("Jarvis-x", "x.local.", "1.2.3.4", 8765, (("name", "<script>alert(1)</script>\x00\x07 ok" + "A" * 300), ("id", "abcdef123456")))
    f = D.collect([(D.build_response(nasty), "1.2.3.4")])[0]
    assert "<" not in f.txt["name"] and ">" not in f.txt["name"] and "\x00" not in f.txt["name"] and len(f.txt["name"]) <= 80


def test_invalid_port_is_ignored():
    zero = D.Announcement("Jarvis-z", "z.local.", "1.2.3.4", 0, (("id", "abcdef123456"),))
    assert D.collect([(D.build_response(zero), "1.2.3.4")]) == []


def test_found_is_never_trusted_only_matched_to_paired_fingerprints():
    found = D.collect([(D.build_response(MINE), "192.168.1.20")])
    paired = {"AB12CD34EF56AABBCCDD": "Office PC"}
    m = D.classify(found, paired)[0]
    assert m.state == "paired" and m.device == "Office PC" and "paired as Office PC" in m.line()
    other = D.classify(found, {"0011223344556677": "Phone"})[0]
    assert other.state == "unpaired" and "NOT paired" in other.line()
    short = D.Announcement("Jarvis-s", "s.local.", "1.2.3.4", 8765, (("id", "ab"),))
    assert D.classify(D.collect([(D.build_response(short), "1.2.3.4")]), {"ab12": "x"})[0].state == "unpaired"     # too short to match anything


class FakeSock:
    def __init__(self, packets):
        self.packets, self.sent, self.closed = list(packets), [], False

    def settimeout(self, t): pass

    def sendto(self, data, addr): self.sent.append((data, addr))

    def recvfrom(self, n):
        if not self.packets:
            raise socket.timeout()
        return self.packets.pop(0)

    def close(self): self.closed = True


def test_scan_sends_a_query_collects_answers_and_closes():
    sock = FakeSock([(D.build_response(MINE), ("192.168.1.20", 5353))])
    ticks = iter([0.0, 0.1, 0.2, 5.0, 5.0, 5.0])
    found = D.scan(1.0, sock_factory=lambda bind: sock, clock=lambda: next(ticks))
    assert [f.instance for f in found] == ["Jarvis-ab12cd34"] and sock.sent[0][1] == (D.MDNS_ADDR, D.MDNS_PORT) and sock.closed


def test_scan_returns_empty_when_multicast_is_blocked():
    def blocked(bind):
        raise OSError("blocked")

    assert D.scan(0.5, sock_factory=blocked) == []


def test_strict_offline_mode_allows_local_multicast_but_not_global_multicast():
    from app.utils.egress import local_address

    assert local_address("224.0.0.251") and local_address("ff02::fb")
    assert not local_address("239.255.255.250") and not local_address("224.1.2.3") and not local_address("8.8.8.8")


def test_device_mesh_lists_known_devices_and_never_trusts_a_discovered_one(tmp_path):
    from app.core import device_mesh as M
    from app.core.devices import DeviceRegistry
    from app.memory.database import Database

    db = Database(tmp_path / "m.db")
    db.initialize()
    DeviceRegistry(db)
    found = D.collect([(D.build_response(MINE), "192.168.1.20")])
    nodes = M.snapshot(db, found)
    unpaired = [n for n in nodes if n.kind == "discovered"]
    assert len(unpaired) == 1 and unpaired[0].trust == "not paired" and unpaired[0].connection == "unpaired"
    assert "pair it" in unpaired[0].warnings[0] and unpaired[0].address == "192.168.1.20:8765"
    assert "NOT" not in M.render([]) and M.render([]) == "No devices known yet."


def test_device_mesh_shows_a_paired_phone_and_attaches_the_discovered_address(db):
    from app.core import device_mesh as M
    from app.remote.mobile import MobileRegistry
    from tests.test_v3150 import paired

    registry = MobileRegistry(db)
    phone = paired(registry)
    device = registry.get(phone.device_id)
    announce = D.Announcement("Jarvis-phone", "p.local.", "192.168.1.55", 8765, (("id", device.fingerprint[:12]), ("name", device.name)))
    nodes = M.snapshot(db, D.collect([(D.build_response(announce), "192.168.1.55")]))
    phones = [n for n in nodes if n.kind == "phone"]
    assert len(phones) == 1 and phones[0].address == "192.168.1.55:8765" and phones[0].trust == device.trust
    assert not [n for n in nodes if n.kind == "discovered"]            # matched to the paired phone, not listed as a stranger
