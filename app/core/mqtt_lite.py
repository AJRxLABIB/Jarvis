"""A tiny, read-only MQTT 3.1.1 subscriber (no dependency): connect to a broker ON YOUR NETWORK, subscribe, collect messages for a few seconds.

Why not a library: only "what do my ESP32 / ESPHome / Zigbee2MQTT sensors say right now" is needed, and a 150-line client is easy to review. It
never publishes application data, never stores credentials (anonymous brokers only; a broker that wants a password is reported, not tried) and only
talks to private / link-local addresses or ``.local`` names (``core/local_home.check_address``). Retained messages arrive at once after subscribing,
so a sensor that reports rarely is still read. Tested against a fake broker on loopback; NOT yet verified against a real broker / sensors.
"""

from __future__ import annotations

import socket
import time
from typing import Callable

PORT = 1883
CONNACK_REASONS = {1: "wrong protocol version", 2: "client id rejected", 3: "broker unavailable", 4: "bad credentials", 5: "needs a password (not supported)"}


class MqttError(Exception):
    pass


def _varlen(n: int) -> bytes:
    out = bytearray()
    while True:
        digit, n = n % 128, n // 128
        out.append(digit | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _str(s: str) -> bytes:
    b = s.encode("utf-8")
    return len(b).to_bytes(2, "big") + b


def connect_packet(client_id: str, keepalive: int = 30) -> bytes:
    body = _str("MQTT") + b"\x04\x02" + keepalive.to_bytes(2, "big") + _str(client_id)       # clean session, no user / password
    return b"\x10" + _varlen(len(body)) + body


def subscribe_packet(packet_id: int, topics: list[str]) -> bytes:
    body = packet_id.to_bytes(2, "big") + b"".join(_str(t) + b"\x00" for t in topics)
    return b"\x82" + _varlen(len(body)) + body


def topic_matches(pattern: str, topic: str) -> bool:
    p, t = pattern.split("/"), topic.split("/")
    for i, part in enumerate(p):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(p) == len(t)


class _Reader:
    def __init__(self, sock: socket.socket) -> None:
        self.sock, self.buf = sock, b""

    def _need(self, n: int, deadline: float) -> bytes | None:
        while len(self.buf) < n:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(4096)
            except (socket.timeout, TimeoutError):
                return None
            except OSError as exc:
                raise MqttError(f"connection lost ({exc})") from exc
            if not chunk:
                raise MqttError("the broker closed the connection")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def packet(self, deadline: float) -> tuple[int, int, bytes] | None:
        head = self._need(1, deadline)
        if head is None:
            return None
        length, shift = 0, 0
        for _ in range(4):
            b = self._need(1, deadline + 2)
            if b is None:
                raise MqttError("truncated packet")
            length |= (b[0] & 0x7F) << shift
            shift += 7
            if not b[0] & 0x80:
                break
        if length > 262_144:
            raise MqttError("packet too large")
        body = self._need(length, deadline + 2) if length else b""
        if body is None:
            raise MqttError("truncated packet")
        return head[0] >> 4, head[0] & 0x0F, body


def collect(host: str, port: int, topics: list[str], seconds: float = 3.0, *, opener: Callable[..., socket.socket] = socket.create_connection,
            max_messages: int = 200) -> dict[str, tuple[bytes, float]]:
    """{topic: (payload, received_at)} - the last message per topic seen within ``seconds`` (retained ones included)."""
    if not topics:
        return {}
    try:
        sock = opener((host, port), timeout=4.0)
    except OSError as exc:
        raise MqttError(f"could not reach the broker at {host}:{port} ({exc})") from exc
    found: dict[str, tuple[bytes, float]] = {}
    try:
        reader = _Reader(sock)
        sock.sendall(connect_packet(f"jarvis-{int(time.time()) % 100000}"))
        got = reader.packet(time.monotonic() + 4.0)
        if got is None or got[0] != 2 or len(got[2]) < 2:
            raise MqttError("the broker did not answer the connection")
        if got[2][1] != 0:
            raise MqttError(f"the broker refused: {CONNACK_REASONS.get(got[2][1], 'code ' + str(got[2][1]))}")
        sock.sendall(subscribe_packet(1, topics))
        deadline, count = time.monotonic() + max(0.5, seconds), 0
        while count < max_messages:
            try:
                pkt = reader.packet(deadline)
            except MqttError:
                if found:                                        # the broker hung up after sending data: keep what arrived
                    break
                raise
            if pkt is None:
                break
            kind, flags, body = pkt
            if kind != 3 or len(body) < 2:                       # only PUBLISH carries data; SUBACK / PINGRESP are ignored
                continue
            tlen = int.from_bytes(body[:2], "big")
            topic, rest = body[2:2 + tlen].decode("utf-8", "replace"), body[2 + tlen:]
            qos = (flags >> 1) & 3
            if qos:
                pid, rest = rest[:2], rest[2:]
                try:
                    sock.sendall(b"\x40\x02" + pid)              # PUBACK for QoS 1 (QoS 2 is not requested)
                except OSError:
                    pass
            if any(topic_matches(p, topic) for p in topics):
                found[topic] = (rest[:4096], time.time())
                count += 1
        try:
            sock.sendall(b"\xe0\x00")
        except OSError:
            pass
    finally:
        sock.close()
    return found
