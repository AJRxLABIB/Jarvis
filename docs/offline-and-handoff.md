# Strict offline mode and task handoff

## Strict offline (`OFFLINE_MODE=on`) - `app/utils/egress.py`
Enforced at the socket, installed at startup (`app/main.py`). While it is on, any connection, datagram or name lookup to an
address outside this PC and your own network is refused before a packet leaves, whichever library asked. Allowed: loopback,
10/8, 172.16/12, 192.168/16, link-local, fc00::/7, `localhost`, single-label names, `*.local`, `*.lan`, `*.home.arpa`,
`*.internal`. Other hostnames are refused WITHOUT a DNS query. The setting is read on every call, so changing it takes effect
immediately. Callers see an ordinary network error (`OfflineViolation` is an `OSError`) and degrade as they already do. Blocks
are counted, logged by host only, and shown under Security > Network. `OFFLINE_MODE=auto` (default) is not strict: it is offline
only while there is no internet.

Limits, stated plainly: it covers Python code in this process. A web proxy configured on the PC is a local address, so a
library that honours proxy settings would reach the proxy, which could forward the request - the Security view warns when a
proxy variable is set. Programs Jarvis launches (a browser, Ollama) are outside the guard; Ollama is local. Raw sockets from a
C extension bypass it. Tests: `tests/test_egress.py` (30), lab cases "offline and handoff".

## Task handoff - `app/core/handoff.py`, tool `handoff`, table `handoffs` (migration 38)
A mission moves to a paired phone (or back) as a signed, expiring, single-use JSON package (wire format v1):
`v, id, mission_id, title, state, device_id, created, expires, steps[{n,title,tool,state}], completed[], checkpoint, next,
pending_approvals[], needs[], references[] (file NAMES), context (redacted), mac` (HMAC-SHA256 with a key kept in the database,
never in the package). Step arguments, credentials, keys and anything the redaction rules recognise never travel.

Receiving validates, in this order: well-formed and small -> signed by this PC and unchanged -> secret scan -> issued by this PC
(stored digest) -> not expired -> for this device -> not revoked -> not used -> the device is still trusted. It then compares the
mission's REAL state: if the finished steps changed, or the mission ended, it is a conflict and nothing resumes (the package is
not consumed). A valid package is consumed by one atomic update (two devices cannot both resume it) and yields a resume plan;
it runs nothing - continuing still goes through `resume_mission` with every approval. Default life 30 minutes (max 24 h).
Revoking a device or a package invalidates unused packages at once. `create` and `accept` ask first (moderate).

Not built: the JARVIS Mobile screen that shows and sends handoffs (needs an APK release and a real phone); automatic
conflict merging (a conflict is reported, you choose). Tests: `tests/test_handoff.py` (21), lab cases.
