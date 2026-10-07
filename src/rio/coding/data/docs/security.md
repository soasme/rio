# Project trust and security

Project provider extensions require trust before the CLI imports them.
Explicit approve/decline flags apply to one run. Saved decisions use
`~/.rio/trust.json`; headless `ask` declines protected project inputs.
User and explicitly selected extensions execute with the user's privileges.

MCP management also checks trust before loading project server configuration.
The standalone MCP client takes an explicit `project_trusted` choice when
loading configuration.

Credentials are kept in the private credential store. Provider configuration
refers to them by provider name.

Trust controls resource loading. It does not sandbox scripts, project files,
processes, network requests, or extensions. Use OS isolation and restricted
credentials when running untrusted code.
