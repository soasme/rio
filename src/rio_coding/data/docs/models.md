# rio providers and models

A provider hosts models; a model is the exact ID accepted by that provider. Use `/login` for durable built-in credentials and `/model` to choose an available model.

## Built-in catalog

rio ships a built-in provider catalog (`rio_coding.provider_catalog`, `rio_coding.catalog_loader`) covering the common hosted providers and their models, generated from models.dev metadata (`rio_coding.models_dev`). Each catalog entry carries the provider's base URL, credential/env-var name, model list, context windows, cost, and -- where the provider supports it -- the thinking levels it accepts and which parameter carries them (see `rio_coding.thinking`).

A user-level `~/.rio/catalog.toml` can add or override providers and models on top of the built-in catalog.

## Metadata and selection rules

Use exact provider/model IDs. Model metadata (context window, cost, reasoning support, modalities) comes from the catalog; rio does not infer these from a live model listing. If a model is missing or its metadata looks stale, add or correct an entry in `~/.rio/catalog.toml` rather than guessing at runtime.

## Thinking levels

rio's reasoning-effort levels are `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, and `max` (`rio_coding.thinking.THINKING_LEVELS`). Each provider's catalog entry declares which of these it supports and maps them to that provider's own parameter -- OpenAI-compatible `reasoning_effort`, Anthropic's thinking-token budget, and so on. Selecting an unsupported level for the current model is an error listing the levels that are supported.

## Changing the built-in catalog

Provider transports, authentication, defaults, and fallback model rows live in `src/rio_coding/data/catalog.toml`. Model metadata sourced from models.dev lives in the checked-in `src/rio_coding/data/models-dev-catalog.json` snapshot. Verify transports and corrections against official provider documentation before editing either file; never guess them.

Startup never requires network: a missing or invalid generated/cached catalog falls back to the bundled `catalog.toml`. A user's `~/.rio/catalog.toml` overlay is applied last, on top of the bundled catalog.
