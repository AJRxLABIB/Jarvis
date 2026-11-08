# Round 69 - local read-only MCP server

Lets another program on THIS PC (Claude Desktop, an IDE agent) call a few Jarvis status tools. Off by default.

## Turn it on
1. `python run.py --mcp-server-token` - shows a token once (only its hash is stored; running it again replaces the token; `--mcp-server-revoke` deletes it).
2. `.env`: `MCP_SERVER_ENABLED=true` (optional `MCP_SERVER_PORT=8766`, `MCP_SERVER_TOOLS=sensors;what_changed`). Restart Jarvis. Without a token nothing is served.
3. Client: POST JSON-RPC to `http://127.0.0.1:8766/mcp` with `Authorization: Bearer <token>`.

## What protects it
- Loopback only (127.0.0.1); requests with another Host (DNS rebinding) or a foreign Origin (a web page) get 403.
- Token required on every request; constant-time compare; revoking applies at once.
- Whitelist: only tools you name (default: sensors, what_changed, situations, system_status, service_impact, plugin_check) that are SAFE, have no argument-dependent risk raise, no confirmation and no network use. Everything else is "unknown or not allowed".
- Calls run through the normal executor as AI-origin calls (validation, permissions, taint, audit) - never as your own command. Replies are secret-redacted and capped (12 KB); 30 calls / minute; 64 KB bodies.
- Only MCP "tools" are served (initialize, ping, tools/list, tools/call).

## Not covered / unverified
Tested with local test clients and hostile HTTP cases (tests/test_mcp_server.py). Not yet tried with Claude Desktop itself (it may need an stdio bridge such as `mcp-remote` for HTTP servers). Any local program that gets the token can read what the whitelist exposes: treat the token like a password.

## Already existing: mDNS announcement
Announcing this PC as `_jarvis._tcp.local.` already exists (`remote/discovery.py`, setting LOCAL_DISCOVERY). Only the Android NSD side (needs a JDK to compile) is still open.
