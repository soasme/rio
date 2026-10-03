"""MCP server configuration: `~/.rio/mcp.json` and, in trusted projects, `.rio/mcp.json`.

Both files use the `mcpServers` shape other MCP clients share, so existing
configurations can be copied over::

    {
      "mcpServers": {
        "fs": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]},
        "docs": {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ${TOKEN}"}},
        "sentry": {"url": "https://mcp.sentry.dev/mcp"}
      }
    }

A project entry replaces the global entry with the same name. A project entry
with only `enabled` turns a global server on or off in that project and keeps
the rest of the global entry.

`${NAME}` and `${NAME:-default}` in `command`, `args`, `env`, `cwd`, `url`,
`headers`, and `oauth.clientSecret` expand from the environment when the server
connects. HTTP servers without an `Authorization` header use OAuth when they
answer 401; sign in with `rio mcp login <server>`.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from rio.coding.paths import RioPaths

Scope = Literal["global", "project"]

SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
OVERRIDE_KEYS = frozenset({"enabled"})
DEFAULT_TIMEOUT_SECONDS = 60.0
_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class McpConfigError(ValueError):
    """An `mcp.json` file or server entry is invalid."""


@dataclass(frozen=True, slots=True)
class McpServer:
    """One validated server entry. `config` is the entry as written, without expansion."""

    name: str
    config: dict[str, Any]
    source: Path
    scope: Scope
    #: Project `mcp.json` that overrides `enabled` of this global server.
    override: Path | None = None

    @property
    def enabled(self) -> bool:
        return self.config.get("enabled", True) is not False

    @property
    def url(self) -> str | None:
        return self.config.get("url")

    @property
    def description(self) -> str | None:
        return self.config.get("description")

    @property
    def timeout(self) -> float:
        return float(self.config.get("timeout", DEFAULT_TIMEOUT_SECONDS))

    @property
    def uses_oauth(self) -> bool:
        """HTTP servers without an `Authorization` header may sign in with OAuth."""
        headers = self.config.get("headers") or {}
        return self.url is not None and not any(k.lower() == "authorization" for k in headers)

    def describe_transport(self) -> str:
        if self.url is not None:
            return self.url
        return " ".join([self.config["command"], *self.config.get("args", ())])


@dataclass(frozen=True, slots=True)
class McpConfig:
    servers: tuple[McpServer, ...] = ()
    errors: tuple[str, ...] = ()
    #: The project file when it exists but was skipped because the project is not trusted.
    ignored_project_file: Path | None = None

    def get(self, name: str) -> McpServer | None:
        return next((server for server in self.servers if server.name == name), None)

    @property
    def enabled(self) -> tuple[McpServer, ...]:
        return tuple(server for server in self.servers if server.enabled)


def global_config_path(paths: RioPaths | None = None) -> Path:
    return (paths or RioPaths()).mcp_config_path


def project_config_path(cwd: Path, paths: RioPaths | None = None) -> Path:
    return (paths or RioPaths()).project_mcp_config_path(cwd)


def load_mcp_config(
    cwd: Path, *, project_trusted: bool, paths: RioPaths | None = None
) -> McpConfig:
    """Load the global file and, when the project is trusted, the project file."""
    servers: dict[str, McpServer] = {}
    errors: list[str] = []
    _read(global_config_path(paths), "global", servers, errors)
    project = project_config_path(cwd, paths)
    ignored = None
    if project_trusted:
        _read(project, "project", servers, errors)
    elif project.is_file():
        ignored = project
    return McpConfig(tuple(servers.values()), tuple(errors), ignored)


def _read(path: Path, scope: Scope, servers: dict[str, McpServer], errors: list[str]) -> None:
    try:
        entries = _read_servers(path)
    except FileNotFoundError:
        return
    except McpConfigError as exc:
        errors.append(str(exc))
        return
    for name, value in entries.items():
        if scope == "project" and isinstance(value, dict) and _is_override(value):
            base = servers.get(name)
            if base is None:
                errors.append(f'{path}: server "{name}" needs "command" or "url"')
            elif set(value) - OVERRIDE_KEYS:
                errors.append(f'{path}: server "{name}": an override can only set "enabled"')
            else:
                try:
                    config = validate_server(name, {**base.config, **value})
                except McpConfigError as exc:
                    errors.append(f"{path}: {exc}")
                else:
                    servers[name] = McpServer(name, config, base.source, base.scope, path)
            continue
        try:
            config = validate_server(name, value)
        except McpConfigError as exc:
            errors.append(f"{path}: {exc}")
            continue
        clash = next(
            (other for other in servers if other != name and attribute(other) == attribute(name)),
            None,
        )
        if clash is not None:
            errors.append(f'{path}: server "{name}" conflicts with "{clash}"')
            continue
        servers[name] = McpServer(name, config, path, scope)


def _is_override(value: dict[str, Any]) -> bool:
    return "command" not in value and "url" not in value and "type" not in value


def attribute(name: str) -> str:
    """The Python attribute a server or tool name is reachable as: `my-server` -> `my_server`."""
    return re.sub(r"\W", "_", name)


def _string_map(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    )


def validate_server(name: str, value: object) -> dict[str, Any]:
    """Return the entry when it is valid; raise `McpConfigError` otherwise."""
    if not SERVER_NAME.match(name):
        raise McpConfigError(f'invalid server name "{name}" (use letters, digits, "_" and "-")')
    if not isinstance(value, dict):
        raise McpConfigError(f'server "{name}" must be an object')

    def fail(message: str) -> McpConfigError:
        return McpConfigError(f'server "{name}": {message}')

    if "enabled" in value and not isinstance(value["enabled"], bool):
        raise fail("enabled must be a boolean")
    if "description" in value and not isinstance(value["description"], str):
        raise fail("description must be a string")
    timeout = value.get("timeout")
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0
    ):
        raise fail("timeout must be a positive number of seconds")
    kind = value.get("type")
    if kind == "sse":
        raise fail("the legacy SSE transport is not supported; use the streamable HTTP URL")
    if isinstance(value.get("url"), str) and kind in (None, "http", "streamable-http"):
        if not re.match(r"^https?://", value["url"]):
            raise fail("url must be an http or https URL")
        if "headers" in value and not _string_map(value["headers"]):
            raise fail("headers must map names to strings")
        if "oauth" in value:
            _validate_oauth(value["oauth"], fail)
        return value
    if isinstance(value.get("command"), str) and kind in (None, "stdio"):
        args = value.get("args", [])
        if not (isinstance(args, list) and all(isinstance(arg, str) for arg in args)):
            raise fail("args must be an array of strings")
        if "env" in value and not _string_map(value["env"]):
            raise fail("env must map names to strings")
        if "cwd" in value and not isinstance(value["cwd"], str):
            raise fail("cwd must be a string")
        return value
    raise McpConfigError(f'server "{name}" needs either "command" (stdio) or "url" (HTTP)')


def _validate_oauth(oauth: object, fail: Any) -> None:
    if not isinstance(oauth, dict):
        raise fail("oauth must be an object")
    for key in ("clientId", "clientSecret", "scope", "clientName"):
        if key in oauth and not isinstance(oauth[key], str):
            raise fail(f"oauth.{key} must be a string")
    port = oauth.get("callbackPort")
    if port is not None and (
        isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536
    ):
        raise fail("oauth.callbackPort must be a port number")
    unknown = set(oauth) - {"clientId", "clientSecret", "scope", "clientName", "callbackPort"}
    if unknown:
        raise fail(f"unknown oauth keys: {', '.join(sorted(unknown))}")


def expand(value: str, environ: dict[str, str] | None = None) -> str:
    """Expand `${NAME}` and `${NAME:-default}`. A missing variable without a default fails."""
    env = os.environ if environ is None else environ

    def substitute(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in env:
            return env[name]
        if default is not None:
            return default
        raise McpConfigError(f"environment variable {name} is not set")

    return _VARIABLE.sub(substitute, value)


def check_variables(config: dict[str, Any]) -> None:
    """Raise `McpConfigError` when a value that is expanded names an unset variable."""
    values = [config.get("command", ""), config.get("url", ""), config.get("cwd", "")]
    values += config.get("args", [])
    values += list(config.get("env", {}).values()) + list(config.get("headers", {}).values())
    values.append(config.get("oauth", {}).get("clientSecret", ""))
    for value in values:
        expand(value)


# -- editing -----------------------------------------------------------------


def _read_servers(path: Path) -> dict[str, Any]:
    data = _read_file(path)
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise McpConfigError(f'{path}: expected an object with an "mcpServers" object')
    return servers


def _read_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as exc:
        raise McpConfigError(f"{path}: {exc}") from None
    if not isinstance(data, dict):
        raise McpConfigError(f'{path}: expected an object with an "mcpServers" object')
    return data


def add_server(path: Path, name: str, config: dict[str, Any]) -> bool:
    """Add or replace a server; keep other content. Returns whether one was replaced."""
    validate_server(name, config)
    data = _read_file(path) if path.exists() else {}
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise McpConfigError(f'{path}: expected an object with an "mcpServers" object')
    replaced = name in servers
    servers[name] = config
    _write(path, data)
    return replaced


def remove_server(path: Path, name: str) -> bool:
    """Remove a server. Returns false when the file does not define it."""
    if not path.exists():
        return False
    data = _read_file(path)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or name not in servers:
        return False
    del servers[name]
    _write(path, data)
    return True


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
