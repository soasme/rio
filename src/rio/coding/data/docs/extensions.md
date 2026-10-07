# Provider extensions

Extensions are Python modules with `setup(api)`. They can register provider
definitions and cached model catalogs. Select a provider with
`rio run --provider NAME --model MODEL`.

When a provider is not in the saved catalog, the CLI looks in built-in extensions,
`~/.rio/extensions`, explicit `--extension PATH` arguments, and approved project
`.rio/extensions`. Use `--approve` or `--no-approve` to control project loading.
Extensions execute Python; approve only code you trust.

The library also exposes tool, command, and event registration for custom hosts.
The one-shot CLI uses provider registrations only. It does not bind UI widgets,
slash commands, or session hooks.
