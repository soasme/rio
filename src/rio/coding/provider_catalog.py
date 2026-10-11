"""Built-in provider catalog for rio login/setup flows."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache
from typing import Literal

from rio.ai.types import JSONValue
from rio.coding.thinking import ThinkingLevel, ThinkingParameter

ProviderKind = Literal[
    "openai-compatible",
    "anthropic",
    "openai-codex",
    "google-generative-ai",
    "mistral-conversations",
]
ProviderApi = Literal[
    "openai-completions",
    "openai-responses",
    "anthropic-messages",
    "openai-codex-responses",
    "google-generative-ai",
    "mistral-conversations",
]
ModelInput = Literal["text", "image"]
ThinkingLevelMap = dict[ThinkingLevel, str | None]
AuthMethod = Literal["api_key", "oauth"]


@dataclass(frozen=True, slots=True)
class ModelCostTier:
    """Model rates that apply up to an optional input-token limit."""

    cost: dict[str, float]
    max_input_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ModelCatalogMetadata:
    """Provider-catalog metadata for a single model."""

    name: str | None = None
    api: ProviderApi | None = None
    base_url: str | None = None
    reasoning: bool | None = None
    input: tuple[ModelInput, ...] = ()
    cost: dict[str, float] | None = None
    cost_tiers: tuple[ModelCostTier, ...] = ()
    context_window: int | None = None
    max_tokens: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    compat: dict[str, JSONValue] = field(default_factory=dict)
    thinking_level_map: ThinkingLevelMap = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderCatalogEntry:
    """A built-in provider rio can present during login."""

    name: str
    display_name: str
    kind: ProviderKind
    base_url: str
    api_key_env: str
    credential_name: str | None
    models: tuple[str, ...]
    default_model: str
    docs_url: str
    api: ProviderApi | None = None
    context_windows: dict[str, int] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    compat: dict[str, JSONValue] = field(default_factory=dict)
    model_metadata: dict[str, ModelCatalogMetadata] = field(default_factory=dict)
    thinking_levels: tuple[ThinkingLevel, ...] | None = None
    thinking_models: tuple[str, ...] = ()
    thinking_default: ThinkingLevel | None = None
    thinking_parameter: ThinkingParameter | None = None
    removed_models: tuple[str, ...] = ()
    auth_methods: tuple[AuthMethod, ...] = ("api_key",)


@cache
def _load_builtin_catalog() -> tuple[ProviderCatalogEntry, ...]:
    # Imported lazily and computed on first access (rather than eagerly at
    # module scope) because catalog_loader imports real classes from this
    # module: an eager computation here would make the import order between
    # provider_catalog and catalog_loader load-bearing.
    from rio.coding.catalog_loader import builtin_catalog

    return builtin_catalog()


def __getattr__(name: str) -> tuple[ProviderCatalogEntry, ...]:
    if name == "BUILTIN_PROVIDER_CATALOG":
        return _load_builtin_catalog()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
