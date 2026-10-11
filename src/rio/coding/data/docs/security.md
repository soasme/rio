# Project trust and security

`rio run` selects configured providers without importing extensions or checking
project trust. Python extension loading is available only to custom library hosts.

MCP management also checks trust before loading project server configuration.
The standalone MCP client takes an explicit `project_trusted` choice when
loading configuration.

Credentials are kept in the private credential store. Provider configuration
refers to them by provider name.

Trust controls resource loading. It does not sandbox scripts, project files,
processes, network requests, or extensions. Use OS isolation and restricted
credentials when running untrusted code.
