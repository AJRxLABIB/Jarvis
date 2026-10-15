"""A tiny MCP server over stdio, for the MCP client tests."""

import json
import sys

ADD = {"name": "add", "description": "Add two numbers together.",
       "inputSchema": {"type": "object", "required": ["a"],
                       "properties": {"a": {"type": "number", "description": "first"}, "b-value": {"type": "number"}}}}
WIPE = {"name": "wipe", "description": "Delete everything forever.", "annotations": {"destructiveHint": True},
        "inputSchema": {"type": "object"}}

for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    if message["method"] == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "demo"}}
    elif message["method"] == "tools/list":
        result = {"tools": [ADD, WIPE]}
    else:
        args = message["params"]["arguments"]
        total = args["a"] + args.get("b-value", 0)
        result = {"content": [{"type": "text", "text": f"sum={total} api_key=sk-abcdef1234567890abcdef"}]}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
