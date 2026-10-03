"""A minimal MCP server over stdio for tests: tools, resources, prompts, and a roots request."""

import json
import sys


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def receive():
    line = sys.stdin.readline()
    return json.loads(line) if line else None


TOOLS = [
    {
        "name": "add",
        "description": "Add two numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
    {
        "name": "echo-text",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
    },
    {"name": "fail", "inputSchema": {"type": "object"}},
    {"name": "roots", "inputSchema": {"type": "object"}},
]

print("not json: servers sometimes log to stdout", flush=True)
while (message := receive()) is not None:
    if "id" not in message:
        continue
    method, params, result = message["method"], message.get("params") or {}, None
    if method == "initialize":
        result = {
            "protocolVersion": params["protocolVersion"],
            "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
            "serverInfo": {"name": "fake", "version": "1"},
            "instructions": "Use add for sums.",
        }
    elif method == "tools/list":
        if params.get("cursor") == "2":
            result = {"tools": TOOLS[2:]}
        else:
            result = {"tools": TOOLS[:2], "nextCursor": "2"}
    elif method == "tools/call":
        name, args = params["name"], params.get("arguments", {})
        if name == "add":
            total = args["a"] + args["b"]
            result = {
                "content": [{"type": "text", "text": str(total)}],
                "structuredContent": {"sum": total},
            }
        elif name == "echo-text":
            result = {"content": [{"type": "text", "text": args.get("text", "")}]}
        elif name == "fail":
            result = {"content": [{"type": "text", "text": "boom"}], "isError": True}
        elif name == "roots":
            send({"jsonrpc": "2.0", "id": "r1", "method": "roots/list"})
            reply = receive()
            result = {"content": [{"type": "text", "text": json.dumps(reply["result"])}]}
    elif method == "resources/list":
        result = {"resources": [{"uri": "mem://a", "name": "a"}]}
    elif method == "resources/templates/list":
        result = {"resourceTemplates": []}
    elif method == "resources/read":
        result = {"contents": [{"uri": params["uri"], "text": "hello"}]}
    elif method == "prompts/list":
        result = {"prompts": [{"name": "greet"}]}
    elif method == "prompts/get":
        text = f"hi {params['arguments'].get('who')}"
        result = {"messages": [{"role": "user", "content": {"type": "text", "text": text}}]}
    if result is None:
        send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "nope"}})
    else:
        send({"jsonrpc": "2.0", "id": message["id"], "result": result})
