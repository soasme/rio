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
entries.

## Catalog keys

A catalog has `schema_version = 1` and one or more `[[providers]]` tables.
Only `name` and `base_url` are required for a new provider. The catalog uses
`kind` for the provider type; omitting it selects `openai-compatible`.

| Provider key | Meaning and default |
| --- | --- |
| `name` | Required provider ID. |
| `base_url` | Required API base URL. |
| `display_name` | Name shown to users; defaults to `name`. |
| `kind` | `openai-compatible` (default), `anthropic`, `openai-codex`, `google-generative-ai`, or `mistral-conversations`. |
| `api` | Protocol override: `openai-completions`, `openai-responses`, `anthropic-messages`, `openai-codex-responses`, `google-generative-ai`, or `mistral-conversations`. Defaults to the protocol for `kind`. |
| `api_key_env` | Environment variable containing the API key. When omitted or unset, Rio uses a blank key. |
| `credential_name` | Name of a saved credential to check before the environment variable; omitted by default. |
| `models` | Nonempty list of model IDs; defaults to `[default_model]` when set, otherwise `[name]`. |
| `default_model` | Model selected without `--model`; defaults to the first entry in `models`. Must appear in `models` when both are set. |
| `docs_url` | Optional documentation URL; empty by default. |
| `context_windows` | Table mapping model IDs to positive context window sizes. |
| `headers` | Table of extra HTTP headers. |
| `compat` | Table of protocol compatibility settings. |
| `model_metadata` | Tables of per-model settings, listed below. Each model ID must appear in `models`. |
| `thinking_levels` | Supported levels: `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`. |
| `thinking_models` | Model IDs that support the provider's `thinking_levels`. |
| `thinking_default` | Default thinking level; must appear in `thinking_levels`. |
| `thinking_parameter` | `reasoning_effort`, `reasoning.effort`, or `anthropic.thinking`. |
| `removed_models` | Model IDs to remove from a built-in provider. |
| `auth_methods` | Login choices: `api_key` and/or `oauth`; defaults to `["api_key"]`. |

Use `[providers.model_metadata."MODEL_ID"]` for per-model settings:

| Model metadata key | Meaning |
| --- | --- |
| `name` | Display name. |
| `api` | Protocol override, using the `api` values above. |
| `base_url` | API base URL override. |
| `reasoning` | Whether the model supports reasoning (`true` or `false`). |
| `input` | Input modalities: `text` and/or `image`. |
| `cost` | Table of nonnegative rates, commonly `input`, `output`, `cacheRead`, and `cacheWrite`. |
| `cost_tiers` | List of tier tables for input-size-dependent rates. |
| `context_window` | Positive context window size. |
| `max_tokens` | Positive maximum output tokens. |
| `headers` | Extra HTTP headers for this model. |
| `compat` | Protocol compatibility settings for this model. |
| `thinking_level_map` | Table mapping Rio thinking levels to provider-specific values. |
| `unsupported_thinking_levels` | List of Rio thinking levels unavailable for this model. |

Each `cost_tiers` entry requires nonnegative `input`, `output`, `cacheRead`,
and `cacheWrite` rates. It may also contain `cacheWrite1h` and a positive
`max_input_tokens` limit. Limits must increase; the final tier omits the limit.

When overriding a built-in provider, you can supply just the fields to change.
Model lists are combined, and `context_windows`, `headers`, `compat`, and
`model_metadata` tables merge with the built-in values.
