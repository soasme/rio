# Use MCP tools

Code Cells can search and call [Model Context Protocol](https://modelcontextprotocol.io)
server tools. Rio does not add one model tool per MCP tool. The model gets two
builtins and loads tool schemas only when it searches, so the prompt stays small.

## Configure servers

Rio reads `~/.rio/mcp.json` and the project's `.mcp.json`. Both use the common
`mcpServers` shape. A project entry replaces a global entry with the same name.

```json
{
  "mcpServers": {
    "fs": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]},
    "docs": {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ${DOCS_TOKEN}"}}
  }
}
```

| Key | Transport | Description |
| --- | --- | --- |
| `command`, `args`, `env` | stdio | Process to start in the project directory. |
| `url`, `headers` | Streamable HTTP | Endpoint and request headers. |
| `oauth` | Streamable HTTP | `clientId`, `clientSecret`, optional `scope`, for OAuth client credentials. |

`$NAME` and `${NAME}` expand from the environment. Other keys are ignored.
Browser OAuth sign-in and the legacy SSE transport are not supported.

## Authenticate without a browser

Rio runs unattended, so it uses the MCP
[OAuth Client Credentials](https://github.com/modelcontextprotocol/ext-auth)
extension instead of a browser sign-in:

```json
{"mcpServers": {"api": {"url": "https://api.example.com/mcp",
  "oauth": {"clientId": "rio", "clientSecret": "${API_SECRET}", "scope": "mcp:read"}}}}
```

When the server answers 401, Rio:

1. Reads `resource_metadata` and `scope` from `WWW-Authenticate`, or tries
   `/.well-known/oauth-protected-resource` (RFC 9728).
2. Fetches the first authorization server's metadata from the OAuth (RFC 8414)
   and OpenID Connect well-known URLs, in the MCP priority order, and checks its `issuer`.
3. Posts `grant_type=client_credentials` with `resource` (RFC 8707) and a scope:
   `oauth.scope`, else the challenge scope, else `scopes_supported`.
   The secret goes in the body when the server lists `client_secret_post`, else in
   HTTP Basic auth.
4. Retries once with the bearer token.

Authorization URLs must use https, except on localhost. `private_key_jwt` is not
supported. A static `Authorization` header in `headers` still works.

A project `.mcp.json` starts processes when a Cell uses MCP. Rio already runs
project commands without asking, so treat it like any other project script.

## Call tools from a code Cell

```python
for tool in mcp_tools("issue"):
    print(tool["name"], tool["description"], tool["input_schema"])
print(mcp_call("github.get_issue", {"owner": "soasme", "repo": "rio", "number": 1}))
```

| Builtin | Returns |
| --- | --- |
| `mcp_tools(query="")` | Tools whose `server.tool` name or description contains every query word. |
| `mcp_call(name, args=None)` | Structured content, else joined text, else content blocks. Raises `RuntimeError` on tool errors. |

Servers connect on first use and stop when the Cell ends. Each Cell is a fresh
process, so it reconnects.
