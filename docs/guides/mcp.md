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

## How the model calls tools

At session start, Rio connects to each enabled server and lists its tools in
the instructions. Cells call them through the preloaded `mcp` object:

```python
issues = mcp.linear.list_issues(query="login bug")
help(mcp.linear.list_issues)       # description and parameters
mcp["my-server"].call("tool-name", {"arg": 1})
mcp.fs.resources(); mcp.fs.read_resource("file:///README.md")
```

A tool returns its structured content, else its text, else its content
blocks, and raises `McpToolError` when it reports an error. Servers connect on
first use inside the kernel. A server that is down or needs sign-in is listed
as unavailable.
