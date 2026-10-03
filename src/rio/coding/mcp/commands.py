"""`rio mcp add|remove|list|login|logout`: manage servers and sign in outside a session."""

from __future__ import annotations

import json
import sys
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio

from rio.coding.mcp import ServerStatus, probe, probe_all
from rio.coding.mcp.config import (
    McpConfig,
    McpConfigError,
    add_server,
    expand,
    global_config_path,
    load_mcp_config,
    project_config_path,
    remove_server,
)
from rio.coding.mcp.oauth import McpAuthStore, McpOAuthError, auth_store_path, sign_in
from rio.coding.paths import RioPaths


class McpCommandError(ValueError):
    """A `rio mcp` command failed; the message says why."""


@dataclass(frozen=True, slots=True)
class AddOptions:
    name: str
    command: Sequence[str] = ()
    url: str | None = None
    local: bool = False
    env: Sequence[str] = ()
    headers: Sequence[str] = ()
    cwd: str | None = None
    description: str | None = None
    oauth: dict[str, Any] = field(default_factory=dict)


def _pairs(option: str, values: Sequence[str]) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for value in values:
        key, separator, rest = value.partition("=")
        if not separator or not key:
            raise McpCommandError(f"--{option} expects KEY=VALUE, got {value!r}")
        pairs[key] = rest
    return pairs


def add(options: AddOptions, cwd: Path, paths: RioPaths | None = None) -> str:
    if (options.url is None) == (not options.command):
        raise McpCommandError("give either --url URL or -- COMMAND [ARGS...]")
    config: dict[str, Any]
    if options.url is not None:
        if options.env or options.cwd:
            raise McpCommandError("--env and --cwd only apply to stdio servers")
        config = {"url": options.url}
        if options.headers:
            config["headers"] = _pairs("header", options.headers)
        if options.oauth:
            config["oauth"] = options.oauth
    else:
        if options.headers or options.oauth:
            raise McpCommandError("--header and --oauth-* only apply to HTTP servers (--url)")
        command, *args = options.command
        config = {"command": command}
        if args:
            config["args"] = list(args)
        if options.env:
            config["env"] = _pairs("env", options.env)
        if options.cwd:
            config["cwd"] = options.cwd
    if options.description:
        config["description"] = options.description
    path = project_config_path(cwd, paths) if options.local else global_config_path(paths)
    try:
        replaced = add_server(path, options.name, config)
    except (McpConfigError, OSError) as exc:
        raise McpCommandError(str(exc)) from None
    scope = "project" if options.local else "global"
    verb = "Replaced" if replaced else "Added"
    lines = [f'{verb} {scope} MCP server "{options.name}" in {path}.']
    hint = "Check it with: rio mcp list"
    if options.url is not None and "headers" not in config:
        hint += f". If it requires sign-in: rio mcp login {options.name}"
    lines.append(hint)
    return "\n".join(lines)


def remove(name: str, *, local: bool, cwd: Path, paths: RioPaths | None = None) -> str:
    path = project_config_path(cwd, paths) if local else global_config_path(paths)
    try:
        removed = remove_server(path, name)
    except (McpConfigError, OSError) as exc:
        raise McpCommandError(str(exc)) from None
    if not removed:
        other = project_config_path(cwd, paths) if not local else global_config_path(paths)
        hint = ""
        try:
            if other.is_file() and name in json.loads(other.read_text()).get("mcpServers", {}):
                hint = f" It is defined in {other}; {'omit' if local else 'use'} --local."
        except (ValueError, AttributeError):
            pass
        raise McpCommandError(f'No MCP server "{name}" in {path}.{hint}')
    return f'Removed {"project" if local else "global"} MCP server "{name}" from {path}.'


def project_trusted(cwd: Path, paths: RioPaths | None = None) -> bool:
    """Whether `rio run` would trust the project, without asking."""
    from rio.coding.project_trust import ProjectTrustCoordinator, ProjectTrustStore
    from rio.coding.shell_config import load_shell_settings

    async def resolve() -> bool:
        coordinator = ProjectTrustCoordinator(ProjectTrustStore(paths))
        _, result = await coordinator.resolve(
            cwd, default=load_shell_settings().default_project_trust, persist=False
        )
        return result.trusted

    return anyio.run(resolve)


def _load(cwd: Path, paths: RioPaths | None) -> McpConfig:
    return load_mcp_config(cwd, project_trusted=project_trusted(cwd, paths), paths=paths)


def _untrusted_note(config: McpConfig) -> str | None:
    if config.ignored_project_file is None:
        return None
    return (
        f"{config.ignored_project_file} is ignored because the project is not trusted. "
        "Run `rio run --approve` in the project to trust it."
    )


def _report(status: ServerStatus | None, server: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "name": server.name,
        "scope": server.scope,
        "source": str(server.source),
        "enabled": server.enabled,
        "transport": server.describe_transport(),
        "state": "disabled",
        "tools": [],
    }
    if server.override is not None:
        report["override"] = str(server.override)
    if status is not None:
        report["state"] = (
            "connected" if status.connected else "needs-login" if status.needs_login else "failed"
        )
        report["tools"] = list(status.tools)
        if status.error is not None:
            report["error"] = status.error
    return report


def list_servers(
    cwd: Path, *, as_json: bool = False, paths: RioPaths | None = None
) -> tuple[str, bool]:
    """Connect to each enabled server. Returns the report and whether all is well."""
    config = _load(cwd, paths)
    store = McpAuthStore(auth_store_path(paths))
    statuses = {status.server.name: status for status in probe_all(config.enabled, cwd, store)}
    reports = [_report(statuses.get(server.name), server) for server in config.servers]
    ok = not config.errors and all(r["state"] in ("connected", "disabled") for r in reports)
    note = _untrusted_note(config)
    if as_json:
        data: dict[str, Any] = {"servers": reports, "errors": list(config.errors)}
        if note:
            data["note"] = note
        return json.dumps(data, indent=2), ok
    lines: list[str] = []
    if not reports and not config.errors:
        lines.append(
            f"No MCP servers configured. Add one with `rio mcp add`, or edit "
            f"{global_config_path(paths)} or .rio/mcp.json."
        )
    for report in reports:
        state = report["state"]
        if state == "connected":
            count = len(report["tools"])
            state = f"connected, {count} tool{'' if count == 1 else 's'}"
        lines.append(f"{report['name']}: {state} ({report['scope']})")
        lines.append(f"  {report['transport']}")
        if "override" in report:
            lines.append(f"  project override: {report['override']}")
        if report["state"] == "needs-login":
            lines.append(f"  sign in with: rio mcp login {report['name']}")
        elif "error" in report:
            lines.append("  " + report["error"].replace("\n", "\n  "))
        if report["tools"]:
            lines.append(f"  tools: {', '.join(report['tools'])}")
    lines.extend(f"config error: {error}" for error in config.errors)
    if note:
        lines.append(note)
    return "\n".join(lines), ok


def _oauth_server(name: str, cwd: Path, paths: RioPaths | None) -> Any:
    config = _load(cwd, paths)
    server = config.get(name)
    if server is None:
        note = _untrusted_note(config)
        configured = ", ".join(s.name for s in config.servers) or "none"
        raise McpCommandError(
            f'No MCP server "{name}".{" " + note if note else ""} Configured: {configured}.'
        )
    if not server.uses_oauth:
        raise McpCommandError(
            f'MCP server "{name}" does not use OAuth. '
            "Only HTTP servers without an Authorization header do."
        )
    return server


def login(
    name: str,
    cwd: Path,
    *,
    timeout: float = 300.0,
    paths: RioPaths | None = None,
    open_url: Callable[[str], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> str:
    server = _oauth_server(name, cwd, paths)
    store = McpAuthStore(auth_store_path(paths))
    status = probe(server, cwd, store)
    if status.connected:
        return f'Already signed in to MCP server "{name}" ({len(status.tools)} tools).'
    if not status.needs_login:
        raise McpCommandError(f'MCP server "{name}" failed to connect: {status.error}')
    say = log or (lambda line: print(line, file=sys.stderr))

    def show(url: str) -> None:
        say(f'Sign in to MCP server "{name}" in your browser:\n{url}')
        (open_url or webbrowser.open)(url)

    oauth = server.config.get("oauth", {})
    try:
        secret = expand(oauth["clientSecret"]) if "clientSecret" in oauth else None
        sign_in(
            name,
            expand(server.url),
            oauth,
            secret,
            store,
            www_authenticate=status.www_authenticate,
            open_url=show,
            timeout=timeout,
        )
    except (McpOAuthError, McpConfigError) as exc:
        raise McpCommandError(f'Sign-in to MCP server "{name}" failed: {exc}') from None
    status = probe(server, cwd, store)
    if not status.connected:
        raise McpCommandError(f"Signed in, but the server still fails: {status.error}")
    return f'Signed in to MCP server "{name}" ({len(status.tools)} tools).'


def logout(name: str, cwd: Path, *, paths: RioPaths | None = None) -> str:
    server = _oauth_server(name, cwd, paths)
    removed = McpAuthStore(auth_store_path(paths)).remove(name, expand(server.url))
    if removed:
        return f'Signed out of MCP server "{name}".'
    return f'No stored credentials for MCP server "{name}".'
