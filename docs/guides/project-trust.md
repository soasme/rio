# Trust project resources

`rio run` reads project instructions from `AGENTS.md` or `--agents-md`. It does
not load project provider extensions or resolve project trust.

MCP management commands check project trust before loading `.rio/mcp.json`,
which can start processes. They use saved decisions in `~/.rio/trust.json` and
`defaultProjectTrust` in `~/.rio/settings.json`. The default `ask` policy declines
project resources without an interactive prompt. User-level `~/.rio/mcp.json`
does not require project trust.

The nearest saved decision wins over the default. Unreadable or incomplete trust
stores deny project configuration. Trust checks do not scan instruction files,
themes, or system prompts. Legacy shell-command-prefix settings are ignored.

The Python MCP client takes an explicit `project_trusted` argument when loading
configuration. See [Use MCP servers](mcp.md).

Trust controls resource loading; it does not sandbox scripts or network access.
