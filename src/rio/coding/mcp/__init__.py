"""MCP configuration, discovery, and connections for coding tools."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from rio.coding.mcp.client import McpAuthRequiredError, McpConnectionError, McpError
from rio.coding.mcp.config import (
    McpConfig,
    McpConfigError,
    McpServer,
    attribute,
    check_variables,
    load_mcp_config,
)
from rio.coding.mcp.connection import create_client
from rio.coding.mcp.oauth import McpAuthStore
from rio.coding.system_prompt import PromptSection


@dataclass(frozen=True, slots=True)
class ServerStatus:
    """What connecting to a server found."""

    server: McpServer
    tools: tuple[str, ...] = ()
    instructions: str | None = None
    error: str | None = None
    #: The challenge of a server that answered 401, for `rio mcp login`.
    www_authenticate: str | None = None
    needs_login: bool = False

    @property
    def connected(self) -> bool:
        return self.error is None


def probe(server: McpServer, cwd: Path, store: McpAuthStore) -> ServerStatus:
    """Connect, list the tools, and disconnect."""
    try:
        client = create_client(server.name, server.config, cwd, store)
    except McpConfigError as exc:
        return ServerStatus(server, error=str(exc))
    try:
        client.connect()
        tools = tuple(tool["name"] for tool in client.list_tools())
        return ServerStatus(server, tools, client.instructions)
    except McpAuthRequiredError as exc:
        return ServerStatus(
            server, error=str(exc), www_authenticate=exc.www_authenticate, needs_login=True
        )
    except (McpConnectionError, McpError, OSError) as exc:
        return ServerStatus(server, error=str(exc) or type(exc).__name__)
    finally:
        client.close()


def probe_all(servers: tuple[McpServer, ...], cwd: Path, store: McpAuthStore) -> list[ServerStatus]:
    if not servers:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(servers))) as pool:
        return list(pool.map(lambda server: probe(server, cwd, store), servers))


PROMPT_INTRO = """Call MCP servers from Python with `rio.coding.mcp.api.Mcp`.
Create a client with the server configuration, working directory and auth store.
Call `client.<server>.<tool>(arg=value)` and close the client after use.
`client.<server>.tools()` lists tool names and input schemas."""


def unavailable_reason(server: McpServer) -> str | None:
    """Why a server cannot connect, from what is known without connecting."""
    try:
        check_variables(server.config)
    except McpConfigError as exc:
        return str(exc)
    return None


def prompt_section(servers: tuple[McpServer, ...]) -> PromptSection | None:
    """The skill-instructions section listing the servers. It does not connect to them."""
    if not servers:
        return None
    lines = [PROMPT_INTRO, ""]
    for server in servers:
        line = f"- `{attribute(server.name)}`"
        if server.description:
            line += f": {server.description}"
        reason = unavailable_reason(server)
        if reason is not None:
            line += f" (unavailable: {reason})"
        lines.append(line)
    return PromptSection(title="MCP servers", body="\n".join(lines))


__all__ = [
    "McpConfig",
    "McpServer",
    "ServerStatus",
    "load_mcp_config",
    "probe",
    "probe_all",
    "prompt_section",
]
