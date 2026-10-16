"""Built-in provider catalog for rio login/setup flows."""

from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass(frozen=True, slots=True)
class ModelCatalogMetadata:
    """Provider-catalog metadata for a single model."""

    api: ProviderApi | None = None
    base_url: str | None = None
    reasoning: bool | None = None
    input: tuple[ModelInput, ...] = ()
    max_tokens: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    compat: dict[str, JSONValue] = field(default_factory=dict)
    thinking_level_map: ThinkingLevelMap = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderCatalogEntry:
    """A built-in provider rio can present during login."""

    name: str
    kind: ProviderKind
    base_url: str
    api_key_env: str
    credential_name: str | None
    models: tuple[str, ...]
    default_model: str
    api: ProviderApi | None = None
    headers: dict[str, str] = field(default_factory=dict)
    compat: dict[str, JSONValue] = field(default_factory=dict)
    model_metadata: dict[str, ModelCatalogMetadata] = field(default_factory=dict)
    thinking_levels: tuple[ThinkingLevel, ...] | None = None
    thinking_models: tuple[str, ...] = ()
    thinking_default: ThinkingLevel | None = None
    thinking_parameter: ThinkingParameter | None = None
    removed_models: tuple[str, ...] = ()
