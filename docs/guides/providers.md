# Configure providers and models

Rio selects a configured provider and model for each run. Log in first:

```bash
rio login anthropic
```

Choose a provider and model for one task with `--provider` and `--model`:

```bash
rio run --provider anthropic --model MODEL "Review this repository."
```

Use `--thinking` to request a supported reasoning-effort level:

```bash
rio run --thinking high "Investigate and fix the failing tests."
```

The supported levels depend on the selected model. Rio reports the accepted
levels when a combination is unsupported.

User provider and model overrides live in `~/.rio/catalog.toml`. Rio ships a
built-in catalog; the user catalog is applied last and can add or override its
entries. `rio run` uses only this catalog and saved provider preferences.
Unknown providers and invalid models are configuration errors. Configure custom
endpoints in `~/.rio/catalog.toml`, including [local servers](local-llama-server.md).
Each run selects one provider and model. `~/.rio/providers.json` holds saved
preferences only, such as the default provider and model; it cannot define
providers. Login updates only `default_provider`, preserving other saved keys.
The retired `thinking_defaults` and `inference_providers` preferences are ignored.
Use `--thinking` to select a thinking level for a run.

Logging in never writes `catalog.toml`, so comments and formatting are preserved.
Older informational keys (`display_name`, `docs_url`,
`context_windows`, `auth_methods`, and model `name`, `cost`, `cost_tiers`,
`context_window`) are accepted but ignored. Model data comes from the bundled
snapshot and your catalog; the old `models-store.json` cache is no longer read.

## Catalog keys

A catalog has `schema_version = 1` and one or more `[[providers]]` tables.
Only `name` and `base_url` are required for a new provider. The catalog uses
`kind` for the provider type; omitting it selects `openai-compatible`.

| Provider key | Meaning and default |
| --- | --- |
| `name` | Required provider ID. |
| `base_url` | Required API base URL. |
| `kind` | `openai-compatible` (default), `anthropic`, `openai-codex`, `google-generative-ai`, or `mistral-conversations`. |
| `api` | Protocol override: `openai-completions`, `openai-responses`, `anthropic-messages`, `openai-codex-responses`, `google-generative-ai`, or `mistral-conversations`. Defaults to the protocol for `kind`. |
| `api_key_env` | Environment variable containing the API key. When omitted or unset, Rio uses a blank key. |
| `credential_name` | Name of a saved credential to check before the environment variable; defaults to the provider `name`. |
| `models` | Nonempty list of model IDs; defaults to `[default_model]` when set, otherwise `[name]`. |
| `default_model` | Model selected without `--model`; defaults to the first entry in `models`. Must appear in `models` when both are set. |
| `headers` | Table of extra HTTP headers. |
| `compat` | Table of protocol compatibility settings. |
| `model_metadata` | Tables of per-model settings, listed below. Each model ID must appear in `models`. |
| `thinking_levels` | Supported levels: `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`. |
| `thinking_models` | Model IDs that support the provider's `thinking_levels`. |
| `thinking_default` | Default thinking level; must appear in `thinking_levels`. |
| `thinking_parameter` | `reasoning_effort`, `reasoning.effort`, or `anthropic.thinking`. |
| `removed_models` | Model IDs to remove from a built-in provider. |

Use `[providers.model_metadata."MODEL_ID"]` for per-model settings:

| Model metadata key | Meaning |
| --- | --- |
| `api` | Protocol override, using the `api` values above. |
| `base_url` | API base URL override. |
| `reasoning` | Whether the model supports reasoning (`true` or `false`). |
| `input` | Input modalities: `text` and/or `image`. |
| `max_tokens` | Positive maximum output tokens. |
| `headers` | Extra HTTP headers for this model. |
| `compat` | Protocol compatibility settings for this model. |
| `thinking_level_map` | Table mapping Rio thinking levels to provider-specific values. |
| `unsupported_thinking_levels` | List of Rio thinking levels unavailable for this model. |

When overriding a built-in provider, you can supply just the fields to change.
Model lists are combined, and `headers`, `compat`, and
`model_metadata` tables merge with the built-in values.
