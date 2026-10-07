# Use MCP servers

Rio connects to [Model Context Protocol](https://modelcontextprotocol.io)
servers and lets the model call their tools from Python cells.

## Configure servers

Servers are read from `~/.rio/mcp.json` and, when the project is trusted,
from `.rio/mcp.json` in the project. Both use the `mcpServers` shape other MCP
clients use:

```json
{
  "mcpServers": {
    "fs": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]},
    "docs": {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ${DOCS_TOKEN}"}},
    "linear": {"url": "https://mcp.linear.app/mcp", "description": "Issue tracker."}
  }
}
```

| Key | Applies to | Description |
| --- | --- | --- |
| `command`, `args`, `env`, `cwd` | stdio | Process to start. A relative `cwd` resolves against the project. |
| `url`, `headers` | HTTP | Streamable HTTP endpoint. The legacy SSE transport is not supported. |
| `oauth` | HTTP | `clientId`, `clientSecret`, `callbackPort`, `scope`, `clientName`, for servers without dynamic client registration. |
| `description` | both | Shown to the model next to the server. |
| `timeout` | both | Seconds per request. Default 60. |
| `enabled` | both | `false` keeps the entry without connecting. |

`${NAME}` and `${NAME:-default}` expand from the environment in `command`,
`args`, `env`, `cwd`, `url`, `headers`, and `oauth.clientSecret`.

A project entry replaces the global one with the same name. A project entry
with only `enabled` turns a global server off or on in that project:

```json
{"mcpServers": {"linear": {"enabled": false}}}
```

A project `.rio/mcp.json` starts processes, so it requires
[project trust](project-trust.md).

Or use the CLI:

```bash
rio mcp add fs -- npx -y @modelcontextprotocol/server-filesystem .
rio mcp add linear --url https://mcp.linear.app/mcp
rio mcp add docs --local --url https://example.com/mcp --header 'Authorization=Bearer ${DOCS_TOKEN}'
rio mcp list
```

## Sign in with OAuth

An HTTP server without an `Authorization` header signs in with OAuth when it
answers 401. Rio runs one-shot and cannot open a browser mid-task, so sign in
first:

```bash
rio mcp login linear
```

Rio discovers the authorization server, registers itself (or uses
`oauth.clientId`), and opens the browser. Tokens are saved in
`~/.rio/mcp-auth.json` and refreshed automatically. `rio mcp logout linear`
deletes them.

## Call tools from Python

Use the Python client explicitly. Declare `rio` in the script's PEP 723
dependencies when running through uv.

```python
from pathlib import Path
from rio.coding.mcp.api import Mcp
from rio.coding.mcp.config import load_mcp_config
from rio.coding.mcp.oauth import McpAuthStore, auth_store_path

cwd = Path.cwd()
config = load_mcp_config(cwd, project_trusted=False)
client = Mcp(
    {server.name: server.config for server in config.enabled},
    cwd, McpAuthStore(auth_store_path()),
)
try:
    print(client.linear.tools())
    issues = client.linear.list_issues(query="login bug")
finally:
    client.close()
```

A tool returns structured content, text, or content blocks. It raises
`McpToolError` when the server reports an error. Connections last for this
script only. The CLI does not preload an MCP object into scripts.
