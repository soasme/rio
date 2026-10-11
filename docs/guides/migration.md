# Migrate to the one-shot runtime

These changes remove the former interactive extension and local-management APIs.
The agent's `CellKind` extension point, configured providers, reasoning settings,
authentication, and MCP clients remain available.

## Providers and local servers

`rio run` resolves providers from the built-in catalog, `~/.rio/catalog.toml`,
and saved provider preferences. Unknown providers and invalid models are errors.
It no longer searches Python extensions after a configuration error.

The run flags `--extension` (`-e`), `--approve` (`-a`), and `--no-approve` are
removed. Define custom HTTP endpoints in the catalog. Start local servers with
their own tools before running Rio; see [local servers](local-llama-server.md).
An active session formerly using an extension provider needs a matching provider
and model in the catalog before it can resume.

## Python APIs

The `rio.coding.extensions`, `local_backends`, `built_in_extensions`, `commands`,
`resources`, and `cancellation` modules are removed, along with
`create_dynamic_model_provider` and the llama.cpp state-path accessors.
Use `rio.agent.CellKind` for custom runnable cells and configured providers for
model requests. User extension files and local-management state are left on disk.

`run_persistent_session` takes keyword arguments after the task text. It no
longer accepts `extension_paths` or `trust_override`.

`ScopedModelConfig` and `ProviderSettings.scoped_models` are removed. Old
`scoped_models` JSON entries are ignored and omitted on subsequent saves.
`Limits.external_wakeup` moves to `Session.create(external_wakeup=...)`;
existing journals retain their recorded policy.

## Catalog metadata

Cost rates, cost tiers, display names, and context-window sizes remain catalog
metadata. They are no longer fields of runtime `ProviderModelMetadata`, and
runtime provider configs no longer expose `context_windows`. Read descriptive
metadata through `rio.coding.catalog_loader.effective_catalog`.

Those fields in legacy `providers.json` definitions are ignored; move custom
values to `catalog.toml` if you need to retain them. Existing catalog values are
preserved when runtime preferences or request settings are saved. Request fields
such as `max_tokens`, image support, headers, and reasoning settings still apply.

## MCP trust

MCP management reads the nearest saved decision in `~/.rio/trust.json`. Without
a saved decision, `defaultProjectTrust` in `~/.rio/settings.json` applies:
`always` allows project configuration; `ask` and `never` decline it without a
prompt. An unreadable, malformed, or incomplete trust store denies project trust.
A malformed fallback setting also denies trust.

The interactive `ProjectTrustCoordinator`, resource detector, prompt types, and
`rio.coding.shell_config` are removed. Use
`rio.coding.project_trust.is_project_trusted(cwd, paths)` to read policy.
The saved trust store remains compatible. `shellCommandPrefix` and
`shell_command_prefix` are ignored; they do not configure cell execution.

## Completed sessions

Resuming a completed session cleans up recorded workers and replays committed
events without loading provider configuration, credentials, or the old project
directory. It returns the saved success or failure result. Cleanup failures stop
replay; an unfinished session still needs its provider and working directory.
