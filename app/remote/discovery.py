"""Local device discovery on your own network: multicast DNS / DNS-SD (RFC 6762 / 6763), written here so no extra package or cloud is involved.

What it does: this PC can *announce* itself as ``_jarvis._tcp.local.`` (name, port, a short public-key fingerprint) and can *scan* for other JARVIS
announcements. What it never does: trust anything it finds. A discovered device is only ever "unpaired" or "matches a paired device"; becoming
trusted needs the existing pairing (one-time code + key exchange, ``remote/mobile.py``), nothing here changes trust.

Everything arriving from the network is untrusted input: packets are size-limited, name compression loops are cut, record counts are capped,
and text is stripped of control characters and truncated before it is shown anywhere. Announcements carry no secrets - the fingerprint is a public
identifier. Pure encode / decode / answer logic is tested; the multicast sockets (``announce`` / ``scan``) need a real network and are
HARDWARE-UNVERIFIED: a firewall or a Wi-Fi network with client isolation can block multicast, and then discovery honestly finds nothing.
"""

from __future__ import annotations

import re
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Callable

SERVICE = "_jarvis._tcp.local."
MDNS_ADDR, MDNS_PORT = "224.0.0.251", 5353
TYPE_A, TYPE_PTR, TYPE_TXT, TYPE_SRV, TYPE_ANY = 1, 12, 16, 33, 255
CLASS_IN, CACHE_FLUSH = 1, 0x8000
MAX_PACKET = 1400            # one UDP datagram on ordinary Ethernet / Wi-Fi
MAX_RECORDS = 40
MAX_LABELS = 20
TTL = 120
_CLEAN = re.compile(r"[^\w .@:/+\-()]", re.U)


def clean(text: str, limit: int = 60) -> str:
    """Make a network-supplied string safe to show: no control characters or markup-ish symbols, limited length."""
    return _CLEAN.sub("", str(text))[:limit].strip()


class DnsError(ValueError):
    pass


# ------------------------------------------------------------------ wire format


def encode_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("utf-8")
        if not 0 < len(raw) < 64:
            raise DnsError(f"bad DNS label: {label!r}")
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def decode_name(data: bytes, pos: int) -> tuple[str, int]:
    """(name, position after it) with compression pointers followed at most MAX_LABELS times (no loops)."""
    labels: list[str] = []
    end = -1
    jumps = 0
    while True:
        if pos >= len(data):
            raise DnsError("name runs past the packet")
        length = data[pos]
        if length == 0:
            pos += 1
            break
        if length & 0xC0 == 0xC0:
            if pos + 1 >= len(data):
                raise DnsError("truncated pointer")
            if end < 0:
                end = pos + 2
            pos = ((length & 0x3F) << 8) | data[pos + 1]
            jumps += 1
            if jumps > MAX_LABELS:
                raise DnsError("name compression loop")
            continue
        if length & 0xC0:
            raise DnsError("unsupported label type")
        pos += 1
        if pos + length > len(data):
            raise DnsError("label runs past the packet")
        labels.append(data[pos:pos + length].decode("utf-8", "replace"))
        if len(labels) > MAX_LABELS:
            raise DnsError("too many labels")
        pos += length
    return ".".join(labels) + ".", (end if end >= 0 else pos)


def _record(name: str, rtype: int, rdata: bytes, ttl: int = TTL, flush: bool = True) -> bytes:
    return encode_name(name) + struct.pack("!HHIH", rtype, CLASS_IN | (CACHE_FLUSH if flush else 0), ttl, len(rdata)) + rdata


def build_query(name: str = SERVICE, qtype: int = TYPE_PTR) -> bytes:
    return struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0) + encode_name(name) + struct.pack("!HH", qtype, CLASS_IN)


@dataclass(frozen=True)
class Announcement:
    instance: str                  # "Jarvis-ab12cd"
    host: str                      # "labib-pc.local."
    address: str                   # dotted IPv4
    port: int
    txt: tuple[tuple[str, str], ...] = ()

    @property
    def full_name(self) -> str:
        return f"{self.instance}.{SERVICE}"


def build_response(a: Announcement) -> bytes:
    """The answer to a PTR question for the service: PTR + SRV + TXT + A in one packet."""
    txt = b"".join(bytes([len(item)]) + item for item in (f"{k}={v}".encode("utf-8")[:200] for k, v in a.txt)) or b"\x00"
    ptr = _record(SERVICE, TYPE_PTR, encode_name(a.full_name), flush=False)
    srv = _record(a.full_name, TYPE_SRV, struct.pack("!HHH", 0, 0, a.port) + encode_name(a.host))
    txt_rec = _record(a.full_name, TYPE_TXT, txt)
    addr = _record(a.host, TYPE_A, socket.inet_aton(a.address))
    body = ptr + srv + txt_rec + addr
    return struct.pack("!HHHHHH", 0, 0x8400, 0, 4, 0, 0) + body


@dataclass
class Record:
    name: str
    rtype: int
    ttl: int
    value: Any                     # PTR: name | SRV: (port, target) | TXT: dict | A: ip string


def parse_message(data: bytes) -> tuple[bool, list[tuple[str, int]], list[Record]]:
    """(is_response, questions, records). Raises DnsError for anything malformed or oversized - callers just ignore the packet."""
    if len(data) < 12 or len(data) > 9000:
        raise DnsError("bad packet size")
    _, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", data[:12])
    if qd > MAX_RECORDS or an + ns + ar > MAX_RECORDS:
        raise DnsError("too many records")
    pos = 12
    questions: list[tuple[str, int]] = []
    for _ in range(qd):
        name, pos = decode_name(data, pos)
        if pos + 4 > len(data):
            raise DnsError("truncated question")
        qtype, _ = struct.unpack("!HH", data[pos:pos + 4])
        pos += 4
        questions.append((name, qtype))
    records: list[Record] = []
    for _ in range(an + ns + ar):
        name, pos = decode_name(data, pos)
        if pos + 10 > len(data):
            raise DnsError("truncated record")
        rtype, _cls, ttl, rdlen = struct.unpack("!HHIH", data[pos:pos + 10])
        pos += 10
        if pos + rdlen > len(data):
            raise DnsError("record data runs past the packet")
        rd = data[pos:pos + rdlen]
        value: Any = None
        if rtype == TYPE_PTR:
            value, _ = decode_name(data, pos)
        elif rtype == TYPE_SRV and rdlen >= 7:
            port = struct.unpack("!H", rd[4:6])[0]
            target, _ = decode_name(data, pos + 6)
            value = (port, target)
        elif rtype == TYPE_TXT:
            value, i = {}, 0
            while i < len(rd):
                n = rd[i]
                item = rd[i + 1:i + 1 + n].decode("utf-8", "replace")
                i += 1 + n
                if "=" in item:
                    k, v = item.split("=", 1)
                    value[clean(k, 20)] = clean(v, 80)
        elif rtype == TYPE_A and rdlen == 4:
            value = socket.inet_ntoa(rd)
        pos += rdlen
        records.append(Record(name, rtype, ttl, value))
    return bool(flags & 0x8000), questions, records


def answer_for(query: bytes, mine: Announcement) -> bytes | None:
    """The response to send for ``query`` (None = not for us / not a question / malformed)."""
    try:
        is_response, questions, _ = parse_message(query)
    except DnsError:
        return None
    if is_response:
        return None
    for name, qtype in questions:
        if name.lower() == SERVICE.lower() and qtype in (TYPE_PTR, TYPE_ANY):
            return build_response(mine)
        if name.lower() == mine.full_name.lower() and qtype in (TYPE_SRV, TYPE_TXT, TYPE_ANY):
            return build_response(mine)
    return None


# ------------------------------------------------------------------ what was found


@dataclass
class Found:
    instance: str
    address: str
    port: int
    txt: dict[str, str] = field(default_factory=dict)
    seen_at: float = 0.0

    @property
    def device_id(self) -> str:
        return self.txt.get("id", "")

    def to_dict(self) -> dict[str, Any]:
        return {"instance": self.instance, "address": self.address, "port": self.port, "name": self.txt.get("name", self.instance),
                "id": self.device_id, "version": self.txt.get("v", "")}


def collect(packets: list[tuple[bytes, str]], *, own_id: str = "", now: float | None = None) -> list[Found]:
    """Merge the responses (``(data, sender ip)``) into one entry per announcing device. Malformed packets and our own announcement are skipped.

    The address used is the one the packet really came from, not the one it claims inside (a spoofed A record cannot redirect a later connection)."""
    now = time.time() if now is None else now
    srv: dict[str, tuple[int, str]] = {}
    txts: dict[str, dict[str, str]] = {}
    senders: dict[str, str] = {}
    instances: list[str] = []
    for data, sender in packets:
        try:
            is_response, _, records = parse_message(data)
        except DnsError:
            continue
        if not is_response:
            continue
        for r in records:
            if r.rtype == TYPE_PTR and r.name.lower() == SERVICE.lower() and isinstance(r.value, str):
                if r.value not in instances:
                    instances.append(r.value)
                senders.setdefault(r.value, sender)
            elif r.rtype == TYPE_SRV and isinstance(r.value, tuple):
                srv[r.name] = r.value
                senders.setdefault(r.name, sender)
            elif r.rtype == TYPE_TXT and isinstance(r.value, dict):
                txts[r.name] = r.value
    found: list[Found] = []
    for full in instances[:MAX_RECORDS]:
        port = srv.get(full, (0, ""))[0]
        txt = txts.get(full, {})
        if not 1 <= port <= 65535 or (own_id and txt.get("id") == own_id):
            continue
        found.append(Found(clean(full.split("." + SERVICE)[0]), senders.get(full, ""), port, txt, now))
    return found


@dataclass
class Match:
    found: Found
    state: str                     # "paired" | "unpaired"
    device: str = ""               # the paired device's name when matched

    def line(self) -> str:
        who = self.found.txt.get("name", self.found.instance)
        if self.state == "paired":
            return f"{who} at {self.found.address}:{self.found.port} - paired as {self.device}"
        return f"{who} at {self.found.address}:{self.found.port} - NOT paired (found on the network only; pair it to trust it)"


def classify(found: list[Found], paired_fingerprints: dict[str, str]) -> list[Match]:
    """Match discoveries to paired devices by announced id == the start of a paired device's public-key fingerprint. Never creates trust."""
    out = []
    for f in found:
        ident = f.device_id.lower()
        hit = next((name for fp, name in paired_fingerprints.items() if ident and len(ident) >= 8 and fp.lower().startswith(ident)), "")
        out.append(Match(f, "paired" if hit else "unpaired", hit))
    return out


# ------------------------------------------------------------------ the network side (HARDWARE-UNVERIFIED)


def local_address() -> str:
    """The address other devices on this network can reach this PC at ('' when it can't be told)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))          # no packet is sent; this only picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return ""


def _multicast_socket(bind: bool) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if bind:
        sock.bind(("", MDNS_PORT))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, struct.pack("4s4s", socket.inet_aton(MDNS_ADDR), socket.inet_aton("0.0.0.0")))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    return sock


def scan(seconds: float = 2.0, *, own_id: str = "", sock_factory: Callable[[bool], socket.socket] = _multicast_socket,
         clock: Callable[[], float] = time.monotonic) -> list[Found]:
    """Ask the network who offers the service and collect answers for ``seconds``. Returns [] (not an error) when multicast is blocked."""
    packets: list[tuple[bytes, str]] = []
    try:
        sock = sock_factory(False)
    except OSError:
        return []
    try:
        sock.settimeout(0.3)
        sock.sendto(build_query(), (MDNS_ADDR, MDNS_PORT))
        end = clock() + seconds
        while clock() < end:
            try:
                data, (ip, _) = sock.recvfrom(MAX_PACKET * 2)
            except socket.timeout:
                continue
            except OSError:
                break
            packets.append((data, ip))
            if len(packets) > 200:
                break
    except OSError:
        pass
    finally:
        sock.close()
    return collect(packets, own_id=own_id)


class Announcer:
    """Answers service questions on the local network until ``stop()``. Runs in its own thread; owns one socket."""

    def __init__(self, mine: Announcement, sock_factory: Callable[[bool], socket.socket] = _multicast_socket) -> None:
        import threading

        self.mine, self._factory = mine, sock_factory
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="jarvis-mdns", daemon=True)
        self.error = ""

    def start(self) -> bool:
        try:
            self._sock = self._factory(True)
        except OSError as exc:
            self.error = f"could not listen for discovery: {exc}"
            return False
        self._sock.settimeout(0.5)
        self._thread.start()
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(MAX_PACKET * 2)
            except socket.timeout:
                continue
            except OSError:
                break
            reply = answer_for(data, self.mine)
            if reply is not None:
                try:
                    self._sock.sendto(reply, (MDNS_ADDR, MDNS_PORT))
                except OSError:
                    pass

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except Exception:
            pass
