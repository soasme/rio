"""MCP tools for code Cells: configured servers, connected lazily, tools only.

Servers come from ``~/.rio/mcp.json`` and the project's ``.mcp.json`` in the common
``mcpServers`` shape; a project entry replaces a global one with the same name. A Cell
searches tools with ``mcp_tools`` and calls one with ``mcp_call``. Connections open on
first use and last for the Cell's process.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from rio.coding.mcp_oauth import McpOAuth, client_credentials_token

PROTOCOL_VERSION = "2025-06-18"


class McpServer(BaseModel):
    """One ``mcpServers`` entry: a stdio ``command`` or a streamable HTTP ``url``."""

    model_config = ConfigDict(extra="ignore")

    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}
    url: str | None = None
    headers: dict[str, str] = {}
    oauth: McpOAuth | None = None


def load_servers(cwd: Path, home: Path) -> dict[str, McpServer]:
    """Merge ``home/mcp.json`` and ``cwd/.mcp.json``; project entries win by name."""
    servers: dict[str, McpServer] = {}
    for path in (home / "mcp.json", cwd / ".mcp.json"):
        if not path.is_file():
            continue
        try:
            entries = json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {})
        except (OSError, ValueError, AttributeError) as exc:
            raise ValueError(f"Cannot read {path}: {exc}") from exc
        for name, entry in entries.items():
            servers[name] = McpServer.model_validate(_expand(entry))
    return servers


def _expand(value: Any) -> Any:
    """Expand ``$VAR`` and ``${VAR}`` in configuration strings."""
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    return value


class McpError(RuntimeError):
    """A server failed, or a tool reported an error."""


class _Connection:
    """One initialized MCP session over stdio or streamable HTTP."""

    def __init__(self, name: str, server: McpServer) -> None:
        self.name = name
        self.server = server
        self.next_id = 0
        self.process: subprocess.Popen[bytes] | None = None
        self.http: httpx.Client | None = None
        self.session_id: str | None = None
        self.authorized = False
        if server.command:
            self.process = subprocess.Popen(
                [server.command, *server.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={**os.environ, **server.env},
            )
        elif server.url:
            self.http = httpx.Client(headers=server.headers, timeout=60)
        else:
            raise McpError(f"MCP server {name} needs command or url")
        self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "rio", "version": "0"},
            },
        )
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def request(self, method: str, params: dict) -> dict:
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params}
        for reply in self.send(message):
            if reply.get("id") == self.next_id:
                if "error" in reply:
                    raise McpError(f"{self.name}: {reply['error'].get('message', reply['error'])}")
                result: dict = reply.get("result", {})
                return result
        raise McpError(f"{self.name}: no response to {method}")

    def send(self, message: dict) -> list[dict]:
        """Send one message; return the messages read until its response, if any."""
        data = json.dumps(message)
        if self.process is not None:
            return self._send_stdio(data, "id" in message)
        assert self.http is not None and self.server.url is not None
        headers = {"Accept": "application/json, text/event-stream"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        headers["Content-Type"] = "application/json"
        response = self.http.post(self.server.url, content=data, headers=headers)
        if response.status_code == 401 and self.server.oauth and not self.authorized:
            self.authorized = True
            token = client_credentials_token(
                self.server.url, self.server.oauth, response.headers.get("www-authenticate", "")
            )
            self.http.headers["Authorization"] = f"Bearer {token}"
            response = self.http.post(self.server.url, content=data, headers=headers)
        if response.is_error:
            raise McpError(f"{self.name}: HTTP {response.status_code}")
        self.session_id = response.headers.get("mcp-session-id", self.session_id)
        if "text/event-stream" in response.headers.get("content-type", ""):
            return [
                json.loads(line[5:])
                for line in response.text.splitlines()
                if line.startswith("data:") and line[5:].strip()
            ]
        return [response.json()] if response.content else []

    def _send_stdio(self, data: str, expect_reply: bool) -> list[dict]:
        assert self.process is not None
        assert self.process.stdin is not None and self.process.stdout is not None
        try:
            self.process.stdin.write(data.encode() + b"\n")
            self.process.stdin.flush()
        except OSError as exc:
            raise McpError(f"{self.name}: server exited") from exc
        if not expect_reply:
            return []
        replies = []
        while line := self.process.stdout.readline():
            if line.strip():
                reply = json.loads(line)
                replies.append(reply)
                if "id" in reply and "method" not in reply:
                    return replies
        raise McpError(f"{self.name}: server exited")

    def tools(self) -> list[dict]:
        tools: list[dict] = []
        params: dict = {}
        while True:
            result = self.request("tools/list", params)
            tools.extend(result.get("tools", []))
            if not result.get("nextCursor"):
                return tools
            params = {"cursor": result["nextCursor"]}

    def close(self) -> None:
        if self.process is not None:
            self.process.kill()
            self.process.wait()
        if self.http is not None:
            self.http.close()


class Mcp:
    """Configured servers for one Cell process, connected on first use."""

    def __init__(self, servers: dict[str, McpServer]) -> None:
        self.servers = servers
        self.connections: dict[str, _Connection] = {}

    def _connect(self, name: str) -> _Connection:
        if name not in self.connections:
            if name not in self.servers:
                raise McpError(f"Unknown MCP server: {name}")
            self.connections[name] = _Connection(name, self.servers[name])
        return self.connections[name]

    def tools(self, query: str = "") -> list[dict]:
        """Return ``server.tool`` names, descriptions and input schemas matching every word."""
        words = query.lower().split()
        found = []
        for server in self.servers:
            for tool in self._connect(server).tools():
                entry = {
                    "name": f"{server}.{tool['name']}",
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("inputSchema", {}),
                }
                text = f"{entry['name']} {entry['description']}".lower()
                if all(word in text for word in words):
                    found.append(entry)
        return found

    def call(self, name: str, args: dict | None = None) -> Any:
        """Call ``server.tool``; return structured content, joined text, or content blocks."""
        server, _, tool = name.partition(".")
        if not tool:
            raise McpError(f"MCP tool name must be server.tool: {name}")
        result = self._connect(server).request(
            "tools/call", {"name": tool, "arguments": args or {}}
        )
        content = result.get("content", [])
        if result.get("isError"):
            raise McpError(_text(content) or f"{name} failed")
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        if all(block.get("type") == "text" for block in content):
            return _text(content)
        return content

    def close(self) -> None:
        for connection in self.connections.values():
            connection.close()
        self.connections.clear()


def _text(content: list[dict]) -> str:
    return "\n".join(block.get("text", "") for block in content if block.get("type") == "text")
