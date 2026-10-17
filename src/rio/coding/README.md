# rio.coding

`rio.coding` supplies the cells and configuration that make `rio.agent` a coding
agent. Each task uses one configured provider and model. Local model servers
are started separately and configured as HTTP endpoints.

## Design principles

Follow these when changing or reviewing this package.

- **Extend the agent, do not fork it.** Coding behaviour arrives through
  `CellKind` values and the `ProcessOwner` subclass. If a feature needs a change in
  `rio.agent`, make it a general extension point there, not a coding special case.
- **Owner records stay in owner collections.** Worker identities and other
  process facts are written with `Session.record`, never into core collections.
- **Commit before effects.** A worker starts user code only after its identity
  is durable, so recovery can always find and stop it.
- **Effects are not undone.** Recovery stops leftover work and reports
  `UnknownExecution`; it never assumes an effect rolled back.
- **Configuration is data.** Provider names, costs, tiers and context windows live in
  the catalog. Runtime provider objects carry only request settings and auth.
- **Secrets never reach logs or the model.** Credentials stay in the credential
  store, and OAuth errors are redacted before they are surfaced.

## Coding cells

[cells.py](cells.py) defines two `CellKind` values:

| Kind | Execution |
| --- | --- |
| `code` | An independent Python script with PEP 723 metadata, run through `uv run --script`. |
| `cmd` | An argument list executed directly, without a shell. |

Pass `cells.KINDS` when creating or opening a coding session, and
`cells.INSTRUCTIONS` and `owner_config={"cwd": ...}` when creating it.
`cells.COMMANDS` maps each kind to the argv that `ProcessOwner` runs. The agent runtime handles admission,
journaling and recovery. See the
[library guide](../../../docs/guides/library-usage.md).

## Execution

| Module | Responsibility |
| --- | --- |
| [owner.py](owner.py) | `ProcessOwner` runs cells and argv validators in supervised process groups under `owner_config["cwd"]`, records worker identities in its `workers` collection, and stops leftover workers on recovery. |
| [worker.py](worker.py) | Gated POSIX worker that starts user code only after the owner commits its identity. |
| [api.py](api.py) | Let uv scripts read committed history and schedule timers. |

## Configuration and services

| Module | Responsibility |
| --- | --- |
| [catalog_loader.py](catalog_loader.py), [provider_catalog.py](provider_catalog.py) | Built-in catalog and user overrides in `~/.rio/catalog.toml`. |
| [provider_config.py](provider_config.py), [thinking.py](thinking.py) | Select provider, model and supported reasoning settings. |
| [provider_runtime.py](provider_runtime.py) | Construct `rio.ai` providers with request settings and authentication. |
| [oauth.py](oauth.py) | Shared OAuth contracts: callbacks, PKCE, callback server, device polling, error redaction. |
| [oauth_anthropic.py](oauth_anthropic.py), [oauth_github_copilot.py](oauth_github_copilot.py), [oauth_openai_codex.py](oauth_openai_codex.py) | Provider login and token refresh. |
| [credentials.py](credentials.py), [oauth_registry.py](oauth_registry.py) | Store credentials and select login flows. |
| [context.py](context.py), [system_prompt.py](system_prompt.py) | Load and format project instructions. |

Runtime provider objects carry request settings. Names, costs, tiers and context
window sizes remain in the catalog, available through `effective_catalog()`.

See [providers](../../../docs/guides/providers.md) for setup. The [bundled references](data/docs/README.md)
are also available in installed packages.
