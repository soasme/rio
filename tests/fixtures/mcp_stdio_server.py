"""A tiny stdio MCP server for tests: echo, add (structured), and fail tools."""

from __future__ import annotations

import json
import sys

TOOLS = [
    {"name": "echo", "description": "Echo text back.", "inputSchema": {"type": "object"}},
    {"name": "add", "description": "Add two numbers.", "inputSchema": {"type": "object"}},
    {"name": "fail", "description": "Always fails.", "inputSchema": {"type": "object"}},
]


def result(name: str, args: dict) -> dict:
    if name == "echo":
        return {"content": [{"type": "text", "text": args["text"]}]}
    if name == "add":
        total = args["a"] + args["b"]
        return {
            "content": [{"type": "text", "text": str(total)}],
            "structuredContent": {"sum": total},
        }
    return {"content": [{"type": "text", "text": "boom"}], "isError": True}


def main() -> None:
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        method, params = message["method"], message.get("params", {})
        if method == "initialize":
            reply: dict = {"protocolVersion": params["protocolVersion"], "capabilities": {}}
        elif method == "tools/list":
            reply = {"tools": TOOLS}
        else:
            reply = result(params["name"], params["arguments"])
        print(json.dumps({"jsonrpc": "2.0", "method": "notifications/message"}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": reply}), flush=True)


if __name__ == "__main__":
    main()
