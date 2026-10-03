"""MCP servers for coding sessions, called from notebook cells through `mcp`.

A session reads `mcp.json` (see `rio.coding.mcp.config`), lists the enabled
servers in its instructions, and installs the `mcp` object (see
`rio.coding.mcp.kernel`) in its kernel, which connects them in the background.
Session start never waits for a server.
"""

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
from rio.coding.mcp.kernel import startup_code
from rio.coding.mcp.oauth import McpAuthStore, auth_store_path
from rio.coding.paths import RioPaths
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


PROMPT_INTRO = """MCP servers are reachable from Python cells through the preloaded `mcp` \
object. They connect in the background when the kernel starts. `print(mcp.<server>)` lists a \
server's tools and instructions; `help(mcp.<server>.<tool>)` shows a tool's description and \
parameters. Call a tool as a function with keyword arguments: `mcp.<server>.<tool>(arg=value)`. \
It returns the tool's structured content, else its text, else a list of content blocks, and \
raises `McpToolError` when the tool reports an error. `mcp.<server>.resources()`, \
`.read_resource(uri)`, `.prompts()`, and `.get_prompt(name, **arguments)` reach resources and \
prompts."""


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


@dataclass(frozen=True, slots=True)
class McpSetup:
    """The instructions section and kernel startup code for a session's MCP servers."""

    section: PromptSection | None = None
    startup: str = ""


def prepare(cwd: Path, *, project_trusted: bool, paths: RioPaths | None = None) -> McpSetup:
    """Load the configuration. Servers connect later, in the kernel; nothing waits for them."""
    config = load_mcp_config(cwd, project_trusted=project_trusted, paths=paths)
    servers = config.enabled
    if not servers:
        return McpSetup()
    configs = {server.name: server.config for server in servers}
    return McpSetup(prompt_section(servers), startup_code(configs, cwd, auth_store_path(paths)))


__all__ = [
    "McpConfig",
    "McpServer",
    "McpSetup",
    "ServerStatus",
    "load_mcp_config",
    "prepare",
    "probe",
    "probe_all",
    "prompt_section",
]
