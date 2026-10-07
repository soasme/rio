"""The `mcp` object scripts call MCP servers through.

    mcp                                  # lists the servers
    mcp.github                           # lists the server's tools
    mcp.github.search_issues(query="x")  # calls a tool
    help(mcp.github.search_issues)       # its description and parameters
    mcp["my-server"].call("tool-name", {"arg": 1})

Servers start connecting when the script starts; using one waits for its
connection. They stay connected for the script's lifetime.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import os
import threading
from pathlib import Path
from typing import Any

from rio.coding.mcp.client import McpClient
from rio.coding.mcp.config import McpConfigError, attribute
from rio.coding.mcp.connection import create_client
from rio.coding.mcp.oauth import McpAuthStore


class McpToolError(RuntimeError):
    """A tool ran and reported an error (`isError`)."""

    def _render_traceback_(self) -> list[str]:
        return [f"McpToolError: {self}"]


def tool_result(result: dict[str, Any]) -> Any:
    """`structuredContent`, else the text, else the content blocks. Raises on `isError`."""
    content = result.get("content") or []
    texts = [block.get("text", "") for block in content if block.get("type") == "text"]
    if result.get("isError"):
        raise McpToolError("\n".join(texts) or json.dumps(content))
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    if len(texts) == len(content):
        return "\n".join(texts)
    return content


_JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _signature(schema: dict[str, Any]) -> inspect.Signature:
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or ())
    parameters = []
    for name in sorted(properties, key=lambda key: key not in required):
        if not name.isidentifier():
            continue
        kind = _JSON_TYPES.get((properties[name] or {}).get("type"), inspect.Parameter.empty)
        default = inspect.Parameter.empty if name in required else None
        parameters.append(
            inspect.Parameter(
                name, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=kind
            )
        )
    return inspect.Signature(parameters)


def _doc(tool: dict[str, Any]) -> str:
    parts = [tool.get("description") or ""]
    schema = tool.get("inputSchema") or {}
    for name, spec in (schema.get("properties") or {}).items():
        mark = " (required)" if name in (schema.get("required") or ()) else ""
        spec = spec or {}
        line = f"  {name}: {spec.get('type', 'any')}{mark}"
        if spec.get("description"):
            line += f" - {spec['description']}"
        if "enum" in spec:
            line += f" one of {spec['enum']}"
        parts.append(line)
    if tool.get("outputSchema"):
        parts.append(f"Returns: {json.dumps(tool['outputSchema'])}")
    return "\n".join(part for part in parts if part)


class ServerProxy:
    """One server's tools as Python functions, plus its resources and prompts."""

    def __init__(self, name: str, client: McpClient) -> None:
        self._name = name
        self.__client = client
        self._tools: dict[str, dict[str, Any]] | None = None
        self._ready = threading.Event()
        self._ready.set()

    def start(self) -> None:
        """Connect and list the tools in the background. Uses of the server wait for it.

        A failure is not kept: the first use connects again and raises the error.
        """
        self._ready.clear()

        def warm() -> None:
            try:
                self._tool_map()
            except Exception:  # noqa: BLE001 - raised again by the first use
                pass
            finally:
                self._ready.set()

        threading.Thread(target=warm, name=f"mcp-{self._name}", daemon=True).start()

    @property
    def _client(self) -> McpClient:
        self._ready.wait()
        return self.__client

    @property
    def instructions(self) -> str | None:
        self._client.connect()
        return self._client.instructions

    def tools(self) -> list[dict[str, Any]]:
        """The server's tools, with their input schemas."""
        self._ready.wait()
        return list(self._tool_map().values())

    def call(self, tool: str, arguments: dict[str, Any] | None = None, /, **kwargs: Any) -> Any:
        """Call a tool by its name on the server."""
        return tool_result(self._client.call_tool(tool, {**(arguments or {}), **kwargs}))

    def resources(self) -> list[dict[str, Any]]:
        return self._client.list_resources()

    def resource_templates(self) -> list[dict[str, Any]]:
        return self._client.list_resource_templates()

    def read_resource(self, uri: str) -> str | list[dict[str, Any]]:
        """The resource's text, or its contents when any part is binary."""
        contents = self._client.read_resource(uri)
        if all("text" in item for item in contents):
            return "\n".join(item["text"] for item in contents)
        return contents

    def prompts(self) -> list[dict[str, Any]]:
        return self._client.list_prompts()

    def get_prompt(self, name: str, /, **arguments: str) -> list[dict[str, Any]]:
        """The prompt's messages."""
        return self._client.get_prompt(name, arguments).get("messages", [])

    def close(self) -> None:
        self.__client.close()

    def _tool_map(self) -> dict[str, dict[str, Any]]:
        if self._tools is None:
            # The background connection calls this before `_ready` is set.
            self._tools = {tool["name"]: tool for tool in self.__client.list_tools()}
        return self._tools

    def _function(self, tool: dict[str, Any]) -> Any:
        name = tool["name"]

        def call(**kwargs: Any) -> Any:
            return self.call(name, kwargs)

        call.__name__ = attribute(name)
        call.__qualname__ = f"mcp.{attribute(self._name)}.{attribute(name)}"
        call.__doc__ = _doc(tool)
        call.__signature__ = _signature(tool.get("inputSchema") or {})  # type: ignore[attr-defined]
        return call

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        self._ready.wait()
        tools = self._tool_map()
        tool = tools.get(name) or next(
            (tool for key, tool in tools.items() if attribute(key) == name), None
        )
        if tool is None:
            raise AttributeError(f"MCP server {self._name} has no tool {name!r}")
        return self._function(tool)

    __getitem__ = __getattr__

    def __dir__(self) -> list[str]:
        self._ready.wait()
        own = ["call", "tools", "resources", "resource_templates", "read_resource", "prompts"]
        return [*own, "get_prompt", "instructions", *(attribute(t) for t in self._tool_map())]

    def __repr__(self) -> str:
        self._ready.wait()
        try:
            names = ", ".join(attribute(name) for name in self._tool_map())
        except Exception as exc:  # noqa: BLE001 - shown instead of the tools
            return f"<mcp server {self._name}: {type(exc).__name__}: {exc}>"
        text = f"<mcp server {self._name}: {names or 'no tools'}>"
        instructions = self.__client.instructions
        return f"{text}\n{instructions.strip()}" if instructions else text


class Mcp:
    """The configured MCP servers, by name."""

    def __init__(
        self,
        servers: dict[str, dict[str, Any]],
        cwd: Path,
        store: McpAuthStore,
        *,
        environ: dict[str, str] | None = None,
    ) -> None:
        self._configs = servers
        self._cwd = cwd
        self._store = store
        self._environ = environ
        self._servers: dict[str, ServerProxy] = {}

    def start(self) -> None:
        """Connect every server in the background."""
        for key in self._configs:
            # A missing variable is raised again when a cell uses the server.
            with contextlib.suppress(McpConfigError):
                self[key].start()

    def __getattr__(self, name: str) -> ServerProxy:
        if name.startswith("_"):
            raise AttributeError(name)
        key = (
            name
            if name in self._configs
            else next((key for key in self._configs if attribute(key) == name), None)
        )
        if key is None:
            raise AttributeError(f"no MCP server {name!r}; configured: {', '.join(self._configs)}")
        if key not in self._servers:
            config = self._configs[key]
            client = create_client(key, config, self._cwd, self._store, environ=self._environ)
            self._servers[key] = ServerProxy(key, client)
        return self._servers[key]

    __getitem__ = __getattr__

    def __dir__(self) -> list[str]:
        return [attribute(name) for name in self._configs]

    def __repr__(self) -> str:
        return f"<mcp servers: {', '.join(attribute(name) for name in self._configs)}>"

    def close(self) -> None:
        for server in self._servers.values():
            server.close()
        self._servers.clear()


def host_environ(environ: dict[str, str] | None = None) -> dict[str, str]:
    """The script's environment without the session venv, for the servers it starts.

    The script runs in a private venv; a server command such as `python` or `uvx`
    must resolve as it would in the user's shell.
    """
    env = dict(os.environ if environ is None else environ)
    venv = env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONNOUSERSITE", None)
    if venv is not None:
        bin_dir = str(Path(venv) / "bin")
        env["PATH"] = os.pathsep.join(
            entry for entry in env.get("PATH", "").split(os.pathsep) if entry != bin_dir
        )
    return env
