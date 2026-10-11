# rio.coding

`rio.coding` supplies the cells and configuration that make `rio.agent` a coding
agent. Each task uses one configured provider and model. Local model servers
are started separately and configured as HTTP endpoints.

## Coding cells

[cells.py](cells.py) defines two `CellKind` values:

| Kind | Execution |
| --- | --- |
| `code` | An independent Python script with PEP 723 metadata, run through `uv run --script`. |
| `cmd` | An argument list executed directly, without a shell. |

Pass `cells.KINDS` when creating or opening a coding session and
`cells.INSTRUCTIONS` when creating it. The agent runtime handles admission,
process supervision, journaling and recovery. See the
[library guide](../../../docs/guides/library-usage.md).

## Configuration and services

| Module | Responsibility |
| --- | --- |
| [catalog_loader.py](catalog_loader.py), [provider_catalog.py](provider_catalog.py) | Built-in catalog and user overrides in `~/.rio/catalog.toml`. |
| [provider_config.py](provider_config.py), [thinking.py](thinking.py) | Select provider, model and supported reasoning settings. |
| [provider_runtime.py](provider_runtime.py) | Construct `rio.ai` providers with request settings and authentication. |
| [credentials.py](credentials.py), [oauth_registry.py](oauth_registry.py) | Store credentials and select login flows. |
| [context.py](context.py), [system_prompt.py](system_prompt.py) | Load and format project instructions. |
| [rendering/](rendering/) | Render committed events as a human transcript. |

Runtime provider objects carry request settings. Names, costs, tiers and context
window sizes remain in the catalog, available through `effective_catalog()`.

See [providers](../../../docs/guides/providers.md) for setup. The [bundled references](data/docs/README.md)
are also available in installed packages.
