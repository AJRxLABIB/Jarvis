"""Round 68: MQTT sensors (fake broker on loopback), honest stale/unknown/invalid readings, plugin capability scan."""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from app.core import mqtt_lite as M, plugin_check as P, sensors as S


def _pub(topic: str, payload: str, qos: int = 0) -> bytes:
    t = topic.encode()
    body = len(t).to_bytes(2, "big") + t + (b"\x00\x01" if qos else b"") + payload.encode()
    return bytes([0x30 | (qos << 1)]) + M._varlen(len(body)) + body


class FakeBroker:
    def __init__(self, messages, connack=0):
        self.srv = socket.socket(); self.srv.bind(("127.0.0.1", 0)); self.srv.listen(1)
        self.port = self.srv.getsockname()[1]; self.messages = messages; self.connack = connack; self.got = b""
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        c, _ = self.srv.accept()
        c.settimeout(3)
        self.got += c.recv(4096)                                     # CONNECT
        c.sendall(b"\x20\x02\x00" + bytes([self.connack]))
        if self.connack:
            c.close(); return
        self.got += c.recv(4096)                                     # SUBSCRIBE
        c.sendall(b"\x90\x03\x00\x01\x00")
        for m in self.messages:
            c.sendall(m)
        time.sleep(0.5); c.close()


def test_topic_matching():
    assert M.topic_matches("home/+/temp", "home/room/temp") and M.topic_matches("home/#", "home/a/b")
    assert not M.topic_matches("home/+/temp", "home/room/hum") and not M.topic_matches("home/room", "home/room/x")


def test_collect_reads_retained_and_qos1_messages():
    b = FakeBroker([_pub("home/room/temp", "23.5"), _pub("home/door", "closed", qos=1), _pub("other/x", "1")])
    got = M.collect("127.0.0.1", b.port, ["home/room/temp", "home/door"], seconds=1.5)
    assert got["home/room/temp"][0] == b"23.5" and got["home/door"][0] == b"closed" and "other/x" not in got
    assert b.got.startswith(b"\x10")                                 # a CONNECT with no user / password flags
    assert b"\x04\x02" in b.got[:14]


def test_broker_that_wants_a_password_is_reported():
    b = FakeBroker([], connack=5)
    with pytest.raises(M.MqttError, match="password"):
        M.collect("127.0.0.1", b.port, ["x"], seconds=1)


def test_unreachable_broker_is_an_error_not_old_data():
    with pytest.raises(M.MqttError):
        M.collect("127.0.0.1", 1, ["x"], seconds=1)


SENSORS = [S.Sensor("room temperature", "home/room/temp", "temperature", "C", max_age_s=600),
           S.Sensor("office door", "home/door", "door", max_age_s=600),
           S.Sensor("humidity", "home/hum", "humidity", key="h", max_age_s=600),
           S.Sensor("motion", "home/pir", "motion", max_age_s=60)]


def test_evaluate_ok_stale_unknown_invalid():
    now = 10_000.0
    msgs = {"home/room/temp": (b"23.5", now - 30), "home/door": (b"OPEN", now - 4000), "home/hum": (b'{"h": 140}', now - 5)}
    r = {x.sensor.name: x for x in S.evaluate(SENSORS, msgs, now)}
    assert r["room temperature"].status == "ok" and r["room temperature"].value == 23.5
    assert r["office door"].status == "stale" and "too old" in r["office door"].text()          # never "the door is open"
    assert r["humidity"].status == "invalid"                                                    # 140 % is impossible
    assert r["motion"].status == "unknown" and "unknown" in r["motion"].text()


def test_parse_words_and_json():
    s = S.Sensor("d", "t", "door")
    assert S.parse(s, b"closed")[1] == "closed" and S.parse(s, b"ON")[1] == "open" and S.parse(s, b"banana")[0] == "invalid"
    h = S.Sensor("h", "t", "humidity", key="humidity")
    assert S.parse(h, b'{"humidity": 41.2, "x": 1}') == ("ok", 41.2, "") and S.parse(h, b"{bad")[0] == "invalid" and S.parse(h, b'{"other": 1}')[0] == "invalid"
    assert S.parse(S.Sensor("t", "t", "temperature"), b"nan")[0] == "invalid"


def test_read_all_uses_only_local_brokers_and_goes_unknown_on_failure(tmp_path):
    (tmp_path / "sensors.json").write_text(json.dumps({"broker": "8.8.8.8:1883", "sensors": [{"name": "t", "topic": "a", "kind": "temperature"}]}))
    called = []
    readings, problem = S.read_all(tmp_path, collector=lambda *a, **k: called.append(a) or {})
    assert not called and "own network" in problem and readings[0].status == "unknown"
    (tmp_path / "sensors.json").write_text(json.dumps({"broker": "192.168.1.5", "sensors": [{"name": "t", "topic": "a", "kind": "temperature"}, {"name": "bad", "topic": "b", "kind": "nuclear"}]}))
    ok, problem = S.read_all(tmp_path, collector=lambda host, port, topics, seconds: {"a": (b"20", time.time())})
    assert problem == "" and [r.status for r in ok] == ["ok"] and S.load(tmp_path)[0] == "192.168.1.5"
    def boom(*a, **k): raise M.MqttError("down")
    assert S.read_all(tmp_path, collector=boom)[0][0].status == "unknown"
    assert S.read_all(tmp_path / "nope")[0] == []


def test_plugin_scan_finds_what_the_code_does():
    src = "import requests, subprocess\nimport os\nfrom ctypes import windll\ndef f():\n    eval('1')\n    os.system('x')\n    import json\n"
    found = P.scan(src)
    assert {"network", "process", "native", "dynamic"} <= set(found) and "filesystem" not in found
    assert P.scan("import json\nx = 1\n") == {}
    with pytest.raises(SyntaxError):
        P.scan("def (")


def test_plugin_check_reports_mismatch_and_never_runs_code(tmp_path):
    (tmp_path / "evil.py").write_text("raise SystemExit('ran!')\nimport socket\n")
    (tmp_path / "evil.plugin.json").write_text(json.dumps({"tools": {}, "capabilities": []}))
    (tmp_path / "good.py").write_text("import json\n")
    (tmp_path / "broken.py").write_text("def (")
    rows = {r["name"]: r for r in P.check(tmp_path)}
    assert rows["evil"]["undeclared"] == ["network"] and rows["good"]["detected"] == {} and rows["broken"]["problem"]
    assert "MISMATCH" in P.describe(list(rows.values())) and "not a guarantee" in P.describe(list(rows.values()))
    assert P.check(tmp_path / "missing") == []
