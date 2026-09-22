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
