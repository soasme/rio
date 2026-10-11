# Trust project MCP configuration

MCP management commands check project trust before loading `.rio/mcp.json`,
which can start processes. User-level `~/.rio/mcp.json` is always available.

Rio uses the nearest saved decision for the project or one of its parent
directories in `~/.rio/trust.json`. A saved decision takes precedence over
`defaultProjectTrust` in `~/.rio/settings.json`.

| Policy without a saved decision | Project configuration |
| --- | --- |
| `always` | Load it. |
| `ask` (default) | Ignore it without prompting. |
| `never` | Ignore it. |

Unreadable, malformed, or incomplete trust stores deny project configuration.
Invalid fallback settings also deny it. Commands run without a trust prompt.

Python callers can use `is_project_trusted(cwd)` from
`rio.coding.project_trust` to read this policy. The same module provides
`ProjectTrustStore().set(canonicalize_project_path(cwd), "trusted")` to save a
decision for an absolute project path; use `"untrusted"` to deny it or `remove`
to clear it.

The Python MCP client takes an explicit `project_trusted` argument when loading
configuration. See [Use MCP servers](mcp.md).

`rio run` reads project instructions from `AGENTS.md` or `--agents-md`
independently of MCP trust. Trust controls configuration loading; it does not
sandbox scripts or network access.
