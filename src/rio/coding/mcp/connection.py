"""Turn a configured server into a client: expand variables, pick a transport and auth."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rio.coding.mcp.client import HttpTransport, McpClient, RequestHandler, StdioTransport
from rio.coding.mcp.config import DEFAULT_TIMEOUT_SECONDS, expand
from rio.coding.mcp.oauth import McpAuthStore, OAuthAuth, configured_client


def create_client(
    name: str,
    config: dict[str, Any],
    cwd: Path,
    store: McpAuthStore,
    *,
    environ: dict[str, str] | None = None,
) -> McpClient:
    """A client for one server entry. It connects on first use.

    A stdio server runs with `environ` (default: this process's environment) plus its `env`.

    Raises `McpConfigError` when a `${NAME}` variable is not set.
    """
    roots = [{"uri": cwd.resolve().as_uri(), "name": cwd.name}]
    timeout = float(config.get("timeout", DEFAULT_TIMEOUT_SECONDS))
    if "url" in config:
        url = expand(config["url"])
        headers = {key: expand(value) for key, value in config.get("headers", {}).items()}
        auth = None
        if not any(key.lower() == "authorization" for key in headers):
            oauth = config.get("oauth", {})
            secret = expand(oauth["clientSecret"]) if "clientSecret" in oauth else None
            auth = OAuthAuth(name, url, store, client=configured_client(oauth, secret))

        def http(handle: RequestHandler) -> HttpTransport:
            return HttpTransport(url, headers=headers, auth=auth, handle_request=handle)

        return McpClient(http, timeout=timeout, roots=roots)

    command = expand(config["command"])
    args = [expand(arg) for arg in config.get("args", ())]
    env = {key: expand(value) for key, value in config.get("env", {}).items()}
    workdir = cwd / expand(config["cwd"]) if "cwd" in config else cwd

    def stdio(handle: RequestHandler) -> StdioTransport:
        return StdioTransport(
            command, args, env=env, base_env=environ, cwd=str(workdir), handle_request=handle
        )

    return McpClient(stdio, timeout=timeout, roots=roots)
