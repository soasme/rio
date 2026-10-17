"""Durable provider configuration for rio coding sessions."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field, replace
from json import JSONDecodeError, dumps, loads
from os import environ
from pathlib import Path
from shutil import copy2
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    PositiveFloat,
    StringConstraints,
    ValidationError,
)

from rio.ai.env import (
    CACHE_RETENTION_LONG,
    CACHE_RETENTION_NONE,
    CACHE_RETENTION_SHORT,
    DEFAULT_OPENAI_COMPATIBLE_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    AnthropicConfig,
    CacheRetention,
    OpenAICompatibleConfig,
)
from rio.ai.google import GoogleGenerativeAIConfig
from rio.ai.mistral import MistralConfig
from rio.coding.catalog_loader import builtin_catalog, effective_catalog
from rio.coding.credentials import FileCredentialStore, credentials_path
from rio.coding.oauth_registry import get_oauth_provider
from rio.coding.paths import RioPaths, atomic_write_text
from rio.coding.provider_catalog import (
    ModelCatalogMetadata,
    ProviderApi,
    ProviderCatalogEntry,
)
from rio.coding.thinking import (
    DEFAULT_THINKING_LEVEL,
    THINKING_LEVELS,
    ThinkingLevel,
    ThinkingParameter,
    anthropic_thinking_budget_for_level,
    normalize_thinking_level,
    reasoning_effort_for_level,
)

DEFAULT_PROVIDER_NAME = "openai"
# The durable runner owns retries for agent runs (``Limits.provider_attempts``),
# journaling each failure and its backoff. Transport retries stay off here so one
# turn does not multiply attempts; standalone ``rio.ai`` use keeps its defaults.
AGENT_TRANSPORT_RETRIES = 0
DEFAULT_MODEL = "gpt-5.4"

# Protocol defaults are provider-independent; catalog compat declares exceptions.
_DEFAULT_COMPAT: dict[str, Any] = {
    "supportsStore": True,
    "supportsReasoningEffort": True,
    "supportsUsageInStreaming": True,
    "maxTokensField": "max_completion_tokens",
    "thinkingFormat": "openai",
    "supportsStrictMode": True,
    "supportsLongCacheRetention": True,
    "supportsCacheControl": False,
    "supportsCacheControlOnTools": True,
}

_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ProviderConfigError(ValueError):
    """Raised when rio provider configuration is invalid."""


@dataclass(frozen=True, slots=True)
class ProviderModelMetadata:
    """Runtime metadata for one configured model."""

    api: ProviderApi | None = None
    base_url: str | None = None
    reasoning: bool | None = None
    max_tokens: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    compat: dict[str, Any] = field(default_factory=dict)
    thinking_level_map: dict[ThinkingLevel, str | None] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Provider definition validated by the catalog, with saved runtime preferences."""

    name: str
    base_url: str = DEFAULT_OPENAI_COMPATIBLE_BASE_URL
    api: ProviderApi | None = None
    api_key_env: str = "OPENAI_API_KEY"
    credential_name: str | None = None
    models: tuple[str, ...] = (DEFAULT_MODEL,)
    default_model: str = DEFAULT_MODEL
    headers: dict[str, str] = field(default_factory=dict)
    compat: dict[str, Any] = field(default_factory=dict)
    model_metadata: dict[str, ProviderModelMetadata] = field(default_factory=dict)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    thinking_levels: tuple[ThinkingLevel, ...] | None = None
    thinking_models: tuple[str, ...] = ()
    thinking_default: ThinkingLevel | None = None
    thinking_parameter: ThinkingParameter | None = None


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """rio provider settings loaded from rio home."""

    default_provider: str = DEFAULT_PROVIDER_NAME
    providers: tuple[ProviderConfig, ...] = field(
        default_factory=lambda: builtin_provider_configs()
    )

    def get_provider(self, name: str | None = None) -> ProviderConfig:
        """Return a configured provider by name."""
        target = name or self.default_provider
        for provider in self.providers:
            if provider.name == target:
                return provider
        raise ProviderConfigError(f"Unknown provider: {target}")


@dataclass(frozen=True, slots=True)
class ProviderSelection:
    """Resolved provider/model selection for a rio run."""

    provider: ProviderConfig
    model: str


def builtin_provider_configs() -> tuple[ProviderConfig, ...]:
    """Return rio's built-in provider configs."""
    return tuple(provider_config_from_entry(entry) for entry in builtin_catalog())


def provider_config_from_catalog_entry(name: str) -> ProviderConfig:
    """Create a durable provider config from a built-in catalog entry."""
    for entry in builtin_catalog():
        if entry.name == name:
            return provider_config_from_entry(entry)
    raise ProviderConfigError(f"Unknown built-in provider: {name}")


def provider_config_from_entry(entry: ProviderCatalogEntry) -> ProviderConfig:
    """Create a durable provider config from a catalog entry."""
    return ProviderConfig(
        api=entry.api,
        compat=dict(entry.compat),
        name=entry.name,
        base_url=entry.base_url,
        api_key_env=entry.api_key_env,
        credential_name=entry.credential_name or entry.name,
        models=entry.models,
        default_model=entry.default_model,
        headers=dict(entry.headers),
        model_metadata=_provider_model_metadata_from_catalog(entry.model_metadata),
        thinking_levels=entry.thinking_levels,
        thinking_models=entry.thinking_models,
        thinking_default=entry.thinking_default,
        thinking_parameter=entry.thinking_parameter,
    )


def _provider_model_metadata_from_catalog(
    model_metadata: dict[str, ModelCatalogMetadata],
) -> dict[str, ProviderModelMetadata]:
    return {
        model: ProviderModelMetadata(
            api=metadata.api,
            base_url=metadata.base_url,
            reasoning=metadata.reasoning,
            max_tokens=metadata.max_tokens,
            headers=dict(metadata.headers),
            compat=dict(metadata.compat),
            thinking_level_map=dict(metadata.thinking_level_map),
        )
        for model, metadata in model_metadata.items()
    }


def provider_settings_path(paths: RioPaths | None = None) -> Path:
    """Return the durable provider settings path."""
    return (paths or RioPaths()).home / "providers.json"


def load_provider_settings(paths: RioPaths | None = None) -> ProviderSettings:
    """Load durable provider settings, falling back to env-compatible defaults."""
    resolved_paths = paths or RioPaths()
    path = provider_settings_path(resolved_paths)
    if not path.exists():
        return ProviderSettings(providers=_effective_provider_configs(resolved_paths))
    try:
        raw = loads(path.read_text(encoding="utf-8"))
    except JSONDecodeError as error:
        raise ProviderConfigError(f"{path}: invalid JSON: {error}") from error
    if not isinstance(raw, dict):
        raise ProviderConfigError("Provider settings must be a JSON object")
    settings = provider_settings_from_json(raw, paths=resolved_paths)
    return _with_catalog_providers(settings, paths=resolved_paths)


def save_default_provider(name: str, paths: RioPaths | None = None) -> None:
    """Change the login default without serializing resolved catalog values."""
    resolved_paths = paths or RioPaths()
    load_provider_settings(resolved_paths).get_provider(name)
    path = provider_settings_path(resolved_paths)
    raw = loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    raw["default_provider"] = name
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup_provider_settings(path)
    atomic_write_text(path, dumps(raw, indent=2, sort_keys=True) + "\n")


def _with_catalog_providers(
    settings: ProviderSettings,
    *,
    paths: RioPaths | None = None,
) -> ProviderSettings:
    """Add catalog providers that have no saved preferences."""
    catalog_configs = {config.name: config for config in _effective_provider_configs(paths)}
    providers = _append_catalog_providers(settings.providers, catalog_configs, paths=paths)
    default_provider = settings.default_provider
    if default_provider not in {provider.name for provider in providers}:
        default_provider = providers[0].name if providers else DEFAULT_PROVIDER_NAME
    return ProviderSettings(
        default_provider=default_provider,
        providers=providers,
    )


def _backup_provider_settings(path: Path) -> None:
    """Copy existing settings to the recovery path when possible."""
    if not path.exists():
        return
    with suppress(OSError):
        copy2(path, path.with_suffix(path.suffix + ".bak"))


def _effective_provider_configs(paths: RioPaths | None = None) -> tuple[ProviderConfig, ...]:
    """Return provider configs for the effective catalog (builtin + user overlay)."""
    return tuple(provider_config_from_entry(entry) for entry in effective_catalog(paths))


def _append_catalog_providers(
    providers: tuple[ProviderConfig, ...],
    catalog_configs: dict[str, ProviderConfig],
    *,
    paths: RioPaths | None,
) -> tuple[ProviderConfig, ...]:
    """Append catalog providers: user-catalog ones always, builtins when credentialed."""
    credential_store = FileCredentialStore(credentials_path(paths) if paths else None)
    builtin_names = {entry.name for entry in builtin_catalog()}
    provider_names = {provider.name for provider in providers}
    appended = list(providers)
    for provider in catalog_configs.values():
        if provider.name in provider_names:
            continue
        if provider.name not in builtin_names or provider_has_usable_credentials(
            provider, credential_reader=credential_store
        ):
            appended.append(provider)
            provider_names.add(provider.name)
    return tuple(appended)


class _Preference(BaseModel):
    """Runtime overrides for one catalog provider. Newer Rio versions may add keys."""

    model_config = ConfigDict(extra="ignore", strict=True, str_strip_whitespace=True)

    default_model: _Text | None = None
    headers: dict[_Text, _Text] | None = None
    timeout_seconds: PositiveFloat | None = None


class _SettingsFile(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, str_strip_whitespace=True)

    schema_version: Literal[2] | None = None
    default_provider: _Text
    provider_preferences: dict[_Text, _Preference] = {}


def provider_settings_from_json(
    data: dict[str, Any],
    *,
    paths: RioPaths | None = None,
) -> ProviderSettings:
    """Parse saved preferences; provider definitions come from the catalog."""
    if "provider_preferences" not in data and "providers" in data:
        raise ProviderConfigError(
            "providers.json no longer defines providers; move providers[] entries "
            "to ~/.rio/catalog.toml"
        )
    try:
        parsed = _SettingsFile.model_validate(data)
    except ValidationError as error:
        problem = error.errors()[0]
        location = ".".join(str(part) for part in problem["loc"])
        raise ProviderConfigError(
            f"Invalid provider settings {location}: {problem['msg']}"
        ) from None
    catalog = {provider.name: provider for provider in _effective_provider_configs(paths)}
    # A catalog entry may be removed independently of its saved preference. Ignore the
    # orphan so one stale entry cannot prevent rio from starting.
    providers = tuple(
        _apply_provider_preference(catalog[name], preference)
        for name, preference in parsed.provider_preferences.items()
        if name in catalog
    )
    return ProviderSettings(default_provider=parsed.default_provider, providers=providers)


def _apply_provider_preference(provider: ProviderConfig, preference: _Preference) -> ProviderConfig:
    changes = preference.model_dump(exclude_none=True)
    if changes.get("default_model") not in provider.models:
        changes.pop("default_model", None)
    return replace(provider, **changes)


def resolve_provider_selection(
    settings: ProviderSettings,
    *,
    provider_name: str | None = None,
    model: str | None = None,
) -> ProviderSelection:
    """Resolve the provider and model for a run."""
    provider = settings.get_provider(provider_name)
    selected_model = model or provider.default_model
    if not selected_model:
        raise ProviderConfigError(f"Provider {provider.name} does not define a default model")
    validate_provider_model(provider, selected_model)
    return ProviderSelection(provider=provider, model=selected_model)


def validate_provider_model(provider: ProviderConfig, model: str) -> None:
    """Raise when ``model`` is not declared by ``provider``."""
    if model in provider.models:
        return
    available = ", ".join(sorted(provider.models)) or "none"
    raise ProviderConfigError(
        f"Model is not configured for provider {provider.name}: {model}. "
        f"Available models: {available}"
    )


def provider_thinking_levels(
    provider: ProviderConfig,
    *,
    model: str | None = None,
) -> tuple[ThinkingLevel, ...]:
    """Return thinking levels supported by a provider/model pair."""
    selected_model = model or provider.default_model
    metadata = _metadata_for_model(provider, selected_model)
    if metadata is not None and metadata.reasoning is False:
        return ()
    if provider.thinking_levels is None:
        if metadata is None or metadata.reasoning is not True:
            return ()
        return _levels_from_thinking_map(metadata.thinking_level_map)
    if provider.thinking_models and selected_model not in provider.thinking_models:
        return ()
    return tuple(
        level
        for level in THINKING_LEVELS
        if (
            level in provider.thinking_levels
            or metadata is not None
            and metadata.thinking_level_map.get(level) is not None
        )
        and (metadata is None or _metadata_supports_thinking_level(metadata, level))
    )


def _levels_from_thinking_map(
    thinking_level_map: dict[ThinkingLevel, str | None],
) -> tuple[ThinkingLevel, ...]:
    return tuple(
        level
        for level in THINKING_LEVELS
        if _thinking_level_map_supports(thinking_level_map, level)
    )


def _metadata_supports_thinking_level(
    metadata: ProviderModelMetadata,
    level: ThinkingLevel,
) -> bool:
    if metadata.reasoning is None and not metadata.thinking_level_map:
        return True
    return _thinking_level_map_supports(metadata.thinking_level_map, level)


def _thinking_level_map_supports(
    thinking_level_map: dict[ThinkingLevel, str | None],
    level: ThinkingLevel,
) -> bool:
    if level in thinking_level_map:
        return thinking_level_map[level] is not None
    return level not in {"xhigh", "max"}


def _metadata_for_model(provider: ProviderConfig, model: str) -> ProviderModelMetadata | None:
    return provider.model_metadata.get(model)


@dataclass(frozen=True, slots=True)
class ModelEndpoint:
    """Effective request settings for one provider/model pair.

    Model metadata overrides provider values. Every adapter reads these, so a
    setting means the same thing whichever protocol serves the model.
    """

    api: ProviderApi
    base_url: str
    headers: dict[str, str]
    compat: dict[str, Any]
    max_tokens: int | None


def resolve_model_endpoint(provider: ProviderConfig, model: str | None = None) -> ModelEndpoint:
    """Resolve model/provider precedence once, before an adapter is selected."""
    metadata = _metadata_for_model(provider, model or provider.default_model)
    if metadata is None:
        metadata = ProviderModelMetadata()
    return ModelEndpoint(
        api=metadata.api or provider.api or "openai-completions",
        base_url=metadata.base_url or provider.base_url,
        headers={**provider.headers, **metadata.headers},
        compat={**_DEFAULT_COMPAT, **provider.compat, **metadata.compat},
        max_tokens=metadata.max_tokens,
    )


def provider_api(provider: ProviderConfig, model: str | None = None) -> ProviderApi:
    """Resolve API: model override, provider override, then chat completions."""
    return resolve_model_endpoint(provider, model).api


def _model_compat(provider: ProviderConfig, model: str | None = None) -> dict[str, Any]:
    return resolve_model_endpoint(provider, model).compat


def provider_model_max_tokens(provider: ProviderConfig, model: str | None = None) -> int | None:
    """Return the catalog output token limit for a model, or None when it is unset."""
    return resolve_model_endpoint(provider, model).max_tokens


def provider_default_thinking_level(
    provider: ProviderConfig,
    *,
    model: str | None = None,
) -> ThinkingLevel | None:
    """Return the preferred thinking level for a provider/model pair."""
    levels = provider_thinking_levels(provider, model=model)
    if not levels:
        return None
    if provider.thinking_default in levels:
        return provider.thinking_default
    if DEFAULT_THINKING_LEVEL in levels:
        return DEFAULT_THINKING_LEVEL
    return levels[0]


def resolve_startup_thinking_level(
    provider: ProviderConfig,
    model: str,
    *,
    preferred: ThinkingLevel = DEFAULT_THINKING_LEVEL,
    cli_override: ThinkingLevel | None = None,
) -> ThinkingLevel | None:
    """Resolve thinking for a run: CLI override, preferred level, then catalog default.

    Explicit ``--thinking`` values must be supported by the selected model.
    Without an override, fall back to the first supported level if needed,
    or return ``None`` when the model has no configurable thinking levels.
    """
    levels = provider_thinking_levels(provider, model=model)
    if cli_override is not None:
        if not levels:
            raise ProviderConfigError(f"Thinking modes are unavailable for {provider.name}:{model}")
        if cli_override not in levels:
            allowed = ", ".join(levels)
            raise ProviderConfigError(
                f'Thinking mode "{cli_override}" is not available for '
                f"{provider.name}:{model}. Available modes: {allowed}"
            )
        return cli_override
    if not levels:
        return None
    if preferred in levels:
        return preferred
    return provider_default_thinking_level(provider, model=model) or levels[0]


def openai_compatible_config_from_provider(
    provider: ProviderConfig,
    *,
    credential_reader: FileCredentialStore | None = None,
    model: str | None = None,
    thinking_level: ThinkingLevel | None = None,
) -> OpenAICompatibleConfig:
    """Build OpenAI-compatible runtime config from durable settings."""
    api_key = _api_key_from_provider(provider, credential_reader=credential_reader)
    selected_model = model or provider.default_model
    endpoint = resolve_model_endpoint(provider, selected_model)
    base_url = endpoint.base_url
    if provider.name == DEFAULT_PROVIDER_NAME and provider.api_key_env == "OPENAI_API_KEY":
        base_url = environ.get("OPENAI_BASE_URL", base_url)
    reasoning_effort = reasoning_effort_from_provider(
        provider,
        model=selected_model,
        thinking_level=thinking_level,
    )
    return OpenAICompatibleConfig(
        api_key=api_key,
        provider_name=provider.name,
        api=endpoint.api,
        base_url=base_url.rstrip("/"),
        headers=endpoint.headers,
        timeout_seconds=provider.timeout_seconds,
        max_retries=AGENT_TRANSPORT_RETRIES,
        reasoning_effort=reasoning_effort,
        reasoning_effort_parameter=provider.thinking_parameter or "reasoning_effort",
        thinking_format=_thinking_format(provider, selected_model),
        compat=endpoint.compat,
        response_provider_header=(
            "x-inference-provider" if provider.name == "huggingface" else None
        ),
        include_reasoning_effort_none=_include_reasoning_effort_none(
            provider,
            model=selected_model,
            thinking_level=thinking_level,
        ),
    )


def anthropic_config_from_provider(
    provider: ProviderConfig,
    *,
    credential_reader: FileCredentialStore | None = None,
    model: str | None = None,
    thinking_level: ThinkingLevel | None = None,
) -> AnthropicConfig:
    """Build Anthropic runtime config from durable settings."""
    api_key = _api_key_from_provider(provider, credential_reader=credential_reader)
    selected_model = model or provider.default_model
    thinking_budget_tokens = _anthropic_thinking_budget_from_provider(
        provider,
        model=selected_model,
        thinking_level=thinking_level,
    )
    endpoint = resolve_model_endpoint(provider, selected_model)
    base_url = _normalize_anthropic_base_url(endpoint.base_url)
    cache_retention, cache_control_on_tools = anthropic_cache_settings(provider, selected_model)
    return AnthropicConfig(
        api_key=api_key,
        provider_name=provider.name,
        base_url=base_url,
        cache_retention=cache_retention,
        cache_control_on_tools=cache_control_on_tools,
        headers=endpoint.headers,
        timeout_seconds=provider.timeout_seconds,
        max_retries=AGENT_TRANSPORT_RETRIES,
        max_tokens=endpoint.max_tokens,
        thinking_budget_tokens=thinking_budget_tokens,
        thinking_effort=_reasoning_effort_from_anthropic_provider(
            provider,
            model=selected_model,
            thinking_level=thinking_level,
        ),
        thinking_mode=_anthropic_thinking_mode(
            provider, selected_model, thinking_level=thinking_level
        ),
        compat=endpoint.compat,
    )


def google_config_from_provider(
    provider: ProviderConfig,
    *,
    credential_reader: FileCredentialStore | None = None,
    model: str | None = None,
    thinking_level: ThinkingLevel | None = None,
) -> GoogleGenerativeAIConfig:
    """Build Google Generative AI runtime config from durable settings."""
    selected_model = model or provider.default_model
    endpoint = resolve_model_endpoint(provider, selected_model)
    return GoogleGenerativeAIConfig(
        api_key=_api_key_from_provider(provider, credential_reader=credential_reader),
        provider_name=provider.name,
        base_url=endpoint.base_url.rstrip("/"),
        headers=endpoint.headers,
        timeout_seconds=provider.timeout_seconds,
        max_retries=AGENT_TRANSPORT_RETRIES,
        reasoning_effort=reasoning_effort_from_provider(
            provider, model=selected_model, thinking_level=thinking_level
        ),
    )


def mistral_config_from_provider(
    provider: ProviderConfig,
    *,
    credential_reader: FileCredentialStore | None = None,
    model: str | None = None,
    thinking_level: ThinkingLevel | None = None,
) -> MistralConfig:
    """Build Mistral runtime config from durable settings."""
    selected_model = model or provider.default_model
    endpoint = resolve_model_endpoint(provider, selected_model)
    return MistralConfig(
        api_key=_api_key_from_provider(provider, credential_reader=credential_reader),
        provider_name=provider.name,
        base_url=endpoint.base_url,
        headers=endpoint.headers,
        timeout_seconds=provider.timeout_seconds,
        max_retries=AGENT_TRANSPORT_RETRIES,
        reasoning_effort=reasoning_effort_from_provider(
            provider, model=selected_model, thinking_level=thinking_level
        ),
    )


def provider_has_usable_credentials(
    provider: ProviderConfig,
    *,
    credential_reader: FileCredentialStore | None = None,
) -> bool:
    """Return whether rio can attempt calls for this provider without prompting setup."""
    if provider.credential_name and credential_reader is not None:
        if (
            get_oauth_provider(provider.name) is not None
            and credential_reader.get_oauth(provider.credential_name) is not None
        ):
            return True
        if credential_reader.get(provider.credential_name):
            return True
    return bool(environ.get(provider.api_key_env))


def reasoning_effort_from_provider(
    provider: ProviderConfig,
    *,
    model: str | None,
    thinking_level: ThinkingLevel | None,
) -> str | None:
    if thinking_level is None or provider.thinking_parameter not in {
        "reasoning_effort",
        "reasoning.effort",
    }:
        return None

    selected_model = model or provider.default_model
    normalized = _supported_thinking_level(provider, selected_model, thinking_level)
    if normalized is None:
        return None
    if provider_api(provider, selected_model) == "openai-codex-responses":
        if normalized == "off":
            return None
        if normalized == "minimal":
            return "low"
    mapped = _metadata_thinking_value(provider, selected_model, normalized)
    if mapped is not None:
        return mapped
    if provider.name == "huggingface" and normalized == "minimal":
        # Hugging Face's router currently accepts low/medium/high/xhigh/max/none
        # for reasoning_effort, but rejects Pi/rio's "minimal" label.
        return "low"
    return reasoning_effort_for_level(normalized)


def _anthropic_thinking_budget_from_provider(
    provider: ProviderConfig,
    *,
    model: str | None,
    thinking_level: ThinkingLevel | None,
) -> int | None:
    if thinking_level is None or provider.thinking_parameter != "anthropic.thinking":
        return None

    selected_model = model or provider.default_model
    if _anthropic_thinking_mode(provider, selected_model) == "adaptive":
        return None

    normalized = _supported_thinking_level(provider, selected_model, thinking_level)
    if normalized is None:
        return None
    return anthropic_thinking_budget_for_level(normalized)


def _supported_thinking_level(
    provider: ProviderConfig,
    model: str,
    thinking_level: ThinkingLevel,
) -> ThinkingLevel | None:
    """Normalize a requested level; None when the model has no thinking levels."""
    levels = provider_thinking_levels(provider, model=model)
    if not levels:
        return None
    normalized = normalize_thinking_level(thinking_level)
    if normalized not in levels:
        available = ", ".join(levels)
        raise ProviderConfigError(
            f"Thinking mode {normalized} is not available for "
            f"{provider.name}:{model}. Available modes: {available}"
        )
    return normalized


def _metadata_thinking_value(
    provider: ProviderConfig,
    model: str,
    level: ThinkingLevel,
) -> str | None:
    metadata = _metadata_for_model(provider, model)
    if metadata is None:
        return None
    value = metadata.thinking_level_map.get(level)
    return value if isinstance(value, str) else None


def _thinking_format(provider: ProviderConfig, model: str) -> str:
    value = _model_compat(provider, model).get("thinkingFormat")
    if isinstance(value, str) and value:
        return value
    return "openai"


def _include_reasoning_effort_none(
    provider: ProviderConfig,
    *,
    model: str,
    thinking_level: ThinkingLevel | None,
) -> bool:
    if thinking_level is None:
        return False
    try:
        normalized = normalize_thinking_level(thinking_level)
    except ValueError:
        return False
    if normalized != "off":
        return False
    return _metadata_thinking_value(provider, model, "off") == "none"


def _reasoning_effort_from_anthropic_provider(
    provider: ProviderConfig,
    *,
    model: str,
    thinking_level: ThinkingLevel | None,
) -> str | None:
    if thinking_level is None:
        return None
    normalized = normalize_thinking_level(thinking_level)
    if normalized == "off":
        return None
    mapped = _metadata_thinking_value(provider, model, normalized)
    return mapped or normalized


def _anthropic_thinking_mode(
    provider: ProviderConfig,
    model: str,
    *,
    thinking_level: ThinkingLevel | None = None,
) -> str:
    compat = _model_compat(provider, model)
    if compat.get("forceAdaptiveThinking") is True:
        if thinking_level is not None and normalize_thinking_level(thinking_level) == "off":
            return "disabled"
        return "adaptive"
    return "budget"


def _normalize_anthropic_base_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        return normalized
    return f"{normalized}/v1"


def anthropic_cache_settings(
    provider: ProviderConfig,
    model: str | None = None,
    *,
    oauth: bool = False,
) -> tuple[CacheRetention, bool]:
    """Resolve prompt-cache settings for one Anthropic-protocol request.

    Capability comes from compat, which layers protocol defaults under the
    catalog provider's compat and then per-model compat. Intent comes from the auth
    mode: subscription OAuth is not billed per token, so it asks for the 1 hour
    TTL, while an API key keeps the shorter default. Capability only ever narrows
    intent, so the two compose without any precedence rule.
    """
    compat = _model_compat(provider, model)
    if compat.get("supportsCacheControl") is False:
        return CACHE_RETENTION_NONE, False
    retention = CACHE_RETENTION_LONG if oauth else CACHE_RETENTION_SHORT
    if retention == CACHE_RETENTION_LONG and compat.get("supportsLongCacheRetention") is False:
        retention = CACHE_RETENTION_SHORT
    return retention, compat.get("supportsCacheControlOnTools") is not False


def _api_key_from_provider(
    provider: ProviderConfig,
    *,
    credential_reader: FileCredentialStore | None,
) -> str:
    if provider.credential_name and credential_reader is not None:
        credential = credential_reader.get(provider.credential_name)
        if credential:
            return credential
        if get_oauth_provider(provider.name) is not None:
            oauth_credential = credential_reader.get_oauth(provider.credential_name)
            if oauth_credential is not None:
                return oauth_credential.access

    return environ.get(provider.api_key_env, "") if provider.api_key_env else ""
