"""Durable provider configuration for rio coding sessions."""

from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass, field, fields, replace
from json import JSONDecodeError, dumps, loads
from os import environ
from pathlib import Path
from shutil import copy2
from typing import Annotated, Any, ClassVar, Literal, cast
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveFloat,
    StringConstraints,
    ValidationError,
)

from rio.ai.env import (
    CACHE_RETENTION_LONG,
    CACHE_RETENTION_NONE,
    CACHE_RETENTION_SHORT,
    DEFAULT_ANTHROPIC_BASE_URL,
    DEFAULT_OPENAI_COMPATIBLE_BASE_URL,
    DEFAULT_OPENAI_COMPATIBLE_MAX_RETRIES,
    DEFAULT_OPENAI_COMPATIBLE_MAX_RETRY_DELAY_SECONDS,
    DEFAULT_OPENAI_COMPATIBLE_TIMEOUT_SECONDS,
    AnthropicConfig,
    CacheRetention,
    OpenAICompatibleConfig,
)
from rio.ai.openai_codex import DEFAULT_OPENAI_CODEX_BASE_URL
from rio.coding.catalog_loader import builtin_catalog, effective_catalog
from rio.coding.credentials import CredentialReader, FileCredentialStore, credentials_path
from rio.coding.oauth_registry import get_oauth_provider
from rio.coding.paths import RioPaths, atomic_write_text
from rio.coding.provider_catalog import (
    ModelCatalogMetadata,
    ProviderApi,
    ProviderCatalogEntry,
    ProviderKind,
)
from rio.coding.thinking import (
    DEFAULT_THINKING_LEVEL,
    THINKING_LEVELS,
    ThinkingLevel,
    ThinkingParameter,
    anthropic_thinking_budget_for_level,
    normalize_thinking_level,
    normalize_thinking_levels,
    reasoning_effort_for_level,
)

DEFAULT_PROVIDER_NAME = "openai"
DEFAULT_MODEL = "gpt-5.4"
PROVIDER_SETTINGS_SCHEMA_VERSION = 2

_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ProviderConfigError(ValueError):
    """Raised when rio provider configuration is invalid."""


@dataclass(frozen=True, slots=True)
class ProviderModelMetadata:
    """Runtime metadata for one configured model."""

    api: ProviderApi | None = None
    base_url: str | None = None
    reasoning: bool | None = None
    input: tuple[str, ...] = ()
    max_tokens: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    compat: dict[str, Any] = field(default_factory=dict)
    thinking_level_map: dict[ThinkingLevel, str | None] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        """Serialize this model metadata to JSON-compatible data."""
        return {
            "api": self.api,
            "base_url": self.base_url,
            "reasoning": self.reasoning,
            "input": list(self.input),
            "max_tokens": self.max_tokens,
            "headers": dict(self.headers),
            "compat": dict(self.compat),
            "thinking_level_map": dict(self.thinking_level_map),
        }


class _ProviderConfigBase:
    """Validation and serialization shared by every durable provider config."""

    __slots__ = ()
    json_type: ClassVar[str]

    def __post_init__(self) -> None:
        provider = cast(ProviderConfig, self)
        _validate_provider_numbers(
            timeout_seconds=provider.timeout_seconds,
            max_retries=provider.max_retries,
            max_retry_delay_seconds=provider.max_retry_delay_seconds,
        )
        _validate_model_metadata(provider.models, provider.model_metadata)
        compat = getattr(provider, "compat", None)
        if compat is not None:
            _validate_json_object(compat, "Provider compat")
        _validate_thinking_config(
            thinking_levels=provider.thinking_levels,
            thinking_models=provider.thinking_models,
            thinking_default=provider.thinking_default,
            thinking_parameter=provider.thinking_parameter,
        )
        _validate_thinking_defaults(provider.thinking_defaults)
        inference_providers = getattr(provider, "inference_providers", None)
        if inference_providers is not None:
            _validate_inference_providers(provider.name, provider.models, inference_providers)

    def to_json(self) -> dict[str, Any]:
        """Serialize this provider config to JSON-compatible data."""
        data: dict[str, Any] = {}
        for item in fields(cast(Any, self)):
            value = getattr(self, item.name)
            if item.name == "model_metadata":
                value = {model: metadata.to_json() for model, metadata in value.items()}
            elif isinstance(value, tuple):
                value = list(value)
            elif isinstance(value, dict):
                value = dict(value)
            data[item.name] = value
            if item.name == "name":
                data["type"] = self.json_type
        return data


@dataclass(frozen=True, slots=True)
class OpenAICompatibleProviderConfig(_ProviderConfigBase):
    """Durable settings for one OpenAI-compatible provider."""

    json_type: ClassVar[str] = "openai-compatible"

    name: str
    base_url: str = DEFAULT_OPENAI_COMPATIBLE_BASE_URL
    api: ProviderApi = "openai-completions"
    api_key_env: str = "OPENAI_API_KEY"
    credential_name: str | None = None
    models: tuple[str, ...] = (DEFAULT_MODEL,)
    default_model: str = DEFAULT_MODEL
    headers: dict[str, str] = field(default_factory=dict)
    compat: dict[str, Any] = field(default_factory=dict)
    model_metadata: dict[str, ProviderModelMetadata] = field(default_factory=dict)
    timeout_seconds: float = DEFAULT_OPENAI_COMPATIBLE_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_OPENAI_COMPATIBLE_MAX_RETRIES
    max_retry_delay_seconds: float = DEFAULT_OPENAI_COMPATIBLE_MAX_RETRY_DELAY_SECONDS
    thinking_levels: tuple[ThinkingLevel, ...] | None = None
    thinking_models: tuple[str, ...] = ()
    thinking_default: ThinkingLevel | None = None
    thinking_parameter: ThinkingParameter | None = None
    thinking_defaults: dict[str, ThinkingLevel] = field(default_factory=dict)
    inference_providers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AnthropicProviderConfig(_ProviderConfigBase):
    """Durable settings for Anthropic's Messages API."""

    json_type: ClassVar[str] = "anthropic"

    name: str = "anthropic"
    base_url: str = DEFAULT_ANTHROPIC_BASE_URL
    api: ProviderApi = "anthropic-messages"
    api_key_env: str = "ANTHROPIC_API_KEY"
    credential_name: str | None = "anthropic"
    models: tuple[str, ...] = ("claude-sonnet-4-6",)
    default_model: str = "claude-sonnet-4-6"
    headers: dict[str, str] = field(default_factory=dict)
    compat: dict[str, Any] = field(default_factory=dict)
    model_metadata: dict[str, ProviderModelMetadata] = field(default_factory=dict)
    timeout_seconds: float = DEFAULT_OPENAI_COMPATIBLE_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_OPENAI_COMPATIBLE_MAX_RETRIES
    max_retry_delay_seconds: float = DEFAULT_OPENAI_COMPATIBLE_MAX_RETRY_DELAY_SECONDS
    thinking_levels: tuple[ThinkingLevel, ...] | None = None
    thinking_models: tuple[str, ...] = ()
    thinking_default: ThinkingLevel | None = None
    thinking_parameter: ThinkingParameter | None = None
    thinking_defaults: dict[str, ThinkingLevel] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OpenAICodexProviderConfig(_ProviderConfigBase):
    """Durable settings for OpenAI Codex subscription OAuth."""

    json_type: ClassVar[str] = "openai-codex"

    name: str = "openai-codex"
    base_url: str = DEFAULT_OPENAI_CODEX_BASE_URL
    api_key_env: str = "OPENAI_CODEX_ACCESS_TOKEN"
    credential_name: str | None = "openai-codex"
    models: tuple[str, ...] = (
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.3-codex",
        "gpt-5.3-codex-spark",
        "gpt-5.2",
    )
    default_model: str = "gpt-5.5"
    headers: dict[str, str] = field(default_factory=dict)
    model_metadata: dict[str, ProviderModelMetadata] = field(default_factory=dict)
    timeout_seconds: float = DEFAULT_OPENAI_COMPATIBLE_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_OPENAI_COMPATIBLE_MAX_RETRIES
    max_retry_delay_seconds: float = DEFAULT_OPENAI_COMPATIBLE_MAX_RETRY_DELAY_SECONDS
    thinking_levels: tuple[ThinkingLevel, ...] | None = None
    thinking_models: tuple[str, ...] = ()
    thinking_default: ThinkingLevel | None = None
    thinking_parameter: ThinkingParameter | None = None
    thinking_defaults: dict[str, ThinkingLevel] = field(default_factory=dict)


type ProviderConfig = (
    OpenAICompatibleProviderConfig | AnthropicProviderConfig | OpenAICodexProviderConfig
)


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

    def to_json(self) -> dict[str, Any]:
        """Serialize runtime preferences to JSON-compatible data."""
        return {
            "schema_version": PROVIDER_SETTINGS_SCHEMA_VERSION,
            "default_provider": self.default_provider,
            "provider_preferences": {
                provider.name: _provider_preference_to_json(provider) for provider in self.providers
            },
        }


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
    return _new_provider_config(
        entry.kind,
        # Catalog anthropic entries always speak the Messages API.
        api=None if entry.kind == "anthropic" else entry.api,
        compat=dict(entry.compat),
        name=entry.name,
        base_url=entry.base_url,
        api_key_env=entry.api_key_env,
        credential_name=entry.credential_name,
        models=entry.models,
        default_model=entry.default_model,
        headers=dict(entry.headers),
        model_metadata=_provider_model_metadata_from_catalog(entry.model_metadata),
        thinking_levels=entry.thinking_levels,
        thinking_models=entry.thinking_models,
        thinking_default=entry.thinking_default,
        thinking_parameter=entry.thinking_parameter,
    )


def _new_provider_config(
    kind: str,
    *,
    api: ProviderApi | None,
    compat: dict[str, Any],
    **values: Any,
) -> ProviderConfig:
    """Build the config class for ``kind``; Codex has no api or compat fields."""
    if kind == "openai-codex":
        return OpenAICodexProviderConfig(**values)
    api = api or _default_api_for_kind(kind)
    if kind == "anthropic":
        return AnthropicProviderConfig(api=api, compat=compat, **values)
    return OpenAICompatibleProviderConfig(api=api, compat=compat, **values)


def _default_api_for_kind(kind: str) -> ProviderApi:
    if kind == "anthropic":
        return "anthropic-messages"
    if kind == "openai-codex":
        return "openai-codex-responses"
    if kind == "google-generative-ai":
        return "google-generative-ai"
    if kind == "mistral-conversations":
        return "mistral-conversations"
    return "openai-completions"


def _provider_model_metadata_from_catalog(
    model_metadata: dict[str, ModelCatalogMetadata],
) -> dict[str, ProviderModelMetadata]:
    return {
        model: ProviderModelMetadata(
            api=metadata.api,
            base_url=metadata.base_url,
            reasoning=metadata.reasoning,
            input=tuple(metadata.input),
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


def save_provider_settings(settings: ProviderSettings, paths: RioPaths | None = None) -> Path:
    """Write durable provider preferences and return the path."""
    resolved_paths = paths or RioPaths()
    path = provider_settings_path(resolved_paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_provider_settings(settings, path=path, backup=True)
    return path


def save_default_provider(name: str, paths: RioPaths | None = None) -> None:
    """Change the login default without serializing resolved catalog values."""
    resolved_paths = paths or RioPaths()
    load_provider_settings(resolved_paths).get_provider(name)
    path = provider_settings_path(resolved_paths)
    raw = loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    raw["default_provider"] = name
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup_provider_settings(path, strict=False)
    atomic_write_text(path, dumps(raw, indent=2, sort_keys=True) + "\n")


def upsert_saved_provider(
    provider: ProviderConfig,
    *,
    set_default: bool = False,
    paths: RioPaths | None = None,
    fallback_settings: ProviderSettings | None = None,
) -> ProviderSettings:
    """Reload settings, upsert one provider entry, persist them, and return them."""
    settings = _load_provider_settings_for_write(paths, fallback_settings=fallback_settings)
    updated = upsert_provider(settings, provider, set_default=set_default)
    save_provider_settings(updated, paths)
    return updated


def _load_provider_settings_for_write(
    paths: RioPaths | None,
    *,
    fallback_settings: ProviderSettings | None = None,
) -> ProviderSettings:
    """Load the latest on-disk settings, falling back only when no file exists."""
    resolved_paths = paths or RioPaths()
    if provider_settings_path(resolved_paths).exists():
        return load_provider_settings(resolved_paths)
    if fallback_settings is not None:
        return fallback_settings
    return load_provider_settings(resolved_paths)


def upsert_provider(
    settings: ProviderSettings,
    provider: ProviderConfig,
    *,
    set_default: bool = False,
) -> ProviderSettings:
    """Return settings with a provider added or replaced."""
    providers_by_name = {item.name: item for item in settings.providers}
    builtin_names = {entry.name for entry in builtin_catalog()}
    if provider.name in providers_by_name and provider.name in builtin_names:
        provider = _merge_provider_config(providers_by_name[provider.name], provider)
    providers_by_name[provider.name] = provider
    default_provider = provider.name if set_default else settings.default_provider
    providers = tuple(providers_by_name[name] for name in sorted(providers_by_name))
    updated = ProviderSettings(
        default_provider=default_provider,
        providers=providers,
    )
    updated.get_provider(default_provider)
    return updated


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


def _backup_provider_settings(path: Path, *, strict: bool) -> None:
    """Copy existing settings to the recovery path, optionally requiring success."""
    if not path.exists():
        return
    if strict:
        copy2(path, path.with_suffix(path.suffix + ".bak"))
        return
    with suppress(OSError):
        copy2(path, path.with_suffix(path.suffix + ".bak"))


def _write_provider_settings(
    settings: ProviderSettings,
    *,
    path: Path,
    backup: bool,
) -> None:
    """Atomically write preferences, optionally retaining the previous file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup:
        _backup_provider_settings(path, strict=False)
    atomic_write_text(path, dumps(settings.to_json(), indent=2, sort_keys=True) + "\n")


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


def _merge_provider_config(existing: ProviderConfig, incoming: ProviderConfig) -> ProviderConfig:
    """Merge a replacement provider config without losing local customizations.

    ``incoming`` supplies the definition; ``existing`` keeps the user's runtime
    preferences, extra headers/compat/metadata, and (as one group) thinking setup.
    Codex keeps the incoming model list; other kinds union both lists.
    """
    if type(existing) is not type(incoming):
        return incoming
    if isinstance(incoming, OpenAICodexProviderConfig):
        models = incoming.models
    else:
        models = _unique_strings((*incoming.models, *existing.models))
    thinking = existing if existing.thinking_levels is not None else incoming
    changes: dict[str, Any] = {
        "models": models,
        "default_model": (
            existing.default_model if existing.default_model in models else incoming.default_model
        ),
        "headers": {**incoming.headers, **existing.headers},
        "model_metadata": _merge_provider_model_metadata(
            incoming.model_metadata,
            existing.model_metadata,
        ),
        "timeout_seconds": existing.timeout_seconds,
        "max_retries": existing.max_retries,
        "max_retry_delay_seconds": existing.max_retry_delay_seconds,
        "thinking_levels": thinking.thinking_levels,
        "thinking_models": thinking.thinking_models,
        "thinking_default": thinking.thinking_default,
        "thinking_parameter": thinking.thinking_parameter,
        "thinking_defaults": existing.thinking_defaults,
    }
    if not isinstance(incoming, OpenAICodexProviderConfig):
        changes["compat"] = {**incoming.compat, **cast(Any, existing).compat}
    if isinstance(existing, OpenAICompatibleProviderConfig):
        changes["inference_providers"] = existing.inference_providers
    return replace(incoming, **changes)


def _merge_provider_model_metadata(
    incoming: dict[str, ProviderModelMetadata],
    existing: dict[str, ProviderModelMetadata],
) -> dict[str, ProviderModelMetadata]:
    merged = dict(incoming)
    for model, metadata in existing.items():
        if model not in merged:
            merged[model] = metadata
            continue
        base = merged[model]
        merged[model] = replace(
            base,
            api=metadata.api or base.api,
            base_url=metadata.base_url or base.base_url,
            reasoning=metadata.reasoning if metadata.reasoning is not None else base.reasoning,
            input=metadata.input or base.input,
            max_tokens=metadata.max_tokens or base.max_tokens,
            headers={**base.headers, **metadata.headers},
            compat={**base.compat, **metadata.compat},
            thinking_level_map={**base.thinking_level_map, **metadata.thinking_level_map},
        )
    return merged


def _unique_strings(values: tuple[str, ...]) -> tuple[str, ...]:
    """Return values with duplicates removed while preserving order."""
    return tuple(dict.fromkeys(values))


def _provider_preference_to_json(provider: ProviderConfig) -> dict[str, Any]:
    """Serialize only runtime preferences for one provider."""
    preference = {
        "default_model": provider.default_model,
        "headers": dict(provider.headers),
        "timeout_seconds": provider.timeout_seconds,
        "max_retries": provider.max_retries,
        "max_retry_delay_seconds": provider.max_retry_delay_seconds,
        "thinking_defaults": dict(provider.thinking_defaults),
    }
    if isinstance(provider, OpenAICompatibleProviderConfig) and provider.inference_providers:
        preference["inference_providers"] = dict(provider.inference_providers)
    return preference


class _Preference(BaseModel):
    """Runtime overrides for one catalog provider. Newer Rio versions may add keys."""

    model_config = ConfigDict(extra="ignore", strict=True, str_strip_whitespace=True)

    default_model: _Text | None = None
    headers: dict[_Text, _Text] | None = None
    timeout_seconds: PositiveFloat | None = None
    max_retries: NonNegativeInt | None = None
    max_retry_delay_seconds: NonNegativeFloat | None = None
    thinking_defaults: dict[_Text, _Text] | None = None
    inference_providers: dict[_Text, _Text] | None = None


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
    if "thinking_defaults" in changes:
        changes["thinking_defaults"] = _available_thinking_defaults(
            provider, changes["thinking_defaults"]
        )
    routes = changes.pop("inference_providers", None)
    if routes is not None and isinstance(provider, OpenAICompatibleProviderConfig):
        routes = {model: route for model, route in routes.items() if model in provider.models}
        _validate_inference_providers(provider.name, provider.models, routes)
        changes["inference_providers"] = routes
    return replace(provider, **changes)


def _available_thinking_defaults(
    provider: ProviderConfig, defaults: dict[str, str]
) -> dict[str, ThinkingLevel]:
    """Keep saved defaults the provider still offers; catalogs change between runs."""
    valid: dict[str, ThinkingLevel] = {}
    for model, value in defaults.items():
        try:
            level = normalize_thinking_level(value)
        except ValueError as exc:
            raise ProviderConfigError(str(exc)) from exc
        if model in provider.models and level in provider_thinking_levels(provider, model=model):
            valid[model] = level
    return valid


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
    return getattr(provider, "model_metadata", {}).get(model)


def _provider_api(provider: ProviderConfig, model: str | None = None) -> ProviderApi | str:
    selected_model = model or provider.default_model
    metadata = _metadata_for_model(provider, selected_model)
    if metadata is not None and metadata.api is not None:
        return metadata.api
    if isinstance(provider, OpenAICodexProviderConfig):
        return "openai-codex-responses"
    return getattr(provider, "api", "openai-completions")


def _model_base_url(provider: ProviderConfig, model: str | None = None) -> str:
    selected_model = model or provider.default_model
    metadata = _metadata_for_model(provider, selected_model)
    return metadata.base_url if metadata is not None and metadata.base_url else provider.base_url


def _model_headers(provider: ProviderConfig, model: str | None = None) -> dict[str, str]:
    selected_model = model or provider.default_model
    metadata = _metadata_for_model(provider, selected_model)
    return {**provider.headers, **(metadata.headers if metadata is not None else {})}


def _model_compat(provider: ProviderConfig, model: str | None = None) -> dict[str, Any]:
    selected_model = model or provider.default_model
    metadata = _metadata_for_model(provider, selected_model)
    return {
        **_detected_compat(provider, selected_model),
        **getattr(provider, "compat", {}),
        **(metadata.compat if metadata is not None else {}),
    }


def _detected_compat(provider: ProviderConfig, model: str) -> dict[str, Any]:
    base_url = _model_base_url(provider, model)
    is_together = provider.name == "together" or "api.together.ai" in base_url
    is_zai = provider.name == "zai" or "api.z.ai" in base_url
    is_moonshot = provider.name in {"moonshotai", "moonshotai-cn"} or "moonshot." in base_url
    is_grok = provider.name == "xai" or "api.x.ai" in base_url
    is_deepseek = provider.name == "deepseek" or "deepseek.com" in base_url
    is_cerebras = provider.name == "cerebras" or "cerebras.ai" in base_url
    is_openrouter = provider.name == "openrouter" or "openrouter.ai" in base_url
    is_nonstandard = is_cerebras or is_grok or is_together or is_deepseek or is_zai or is_moonshot
    use_max_tokens = is_moonshot or is_together
    is_anthropic_api = urlsplit(base_url).hostname == urlsplit(DEFAULT_ANTHROPIC_BASE_URL).hostname
    return {
        "supportsStore": not is_nonstandard,
        "supportsReasoningEffort": not (is_grok or is_zai or is_moonshot or is_together),
        "supportsUsageInStreaming": True,
        "maxTokensField": "max_tokens" if use_max_tokens else "max_completion_tokens",
        "thinkingFormat": (
            "deepseek"
            if is_deepseek
            else "zai"
            if is_zai
            else "together"
            if is_together
            else "openrouter"
            if is_openrouter
            else "openai"
        ),
        "supportsStrictMode": not (is_moonshot or is_together),
        "supportsLongCacheRetention": not is_together,
        # Only first-party Anthropic is known to accept cache_control. Several
        # catalog providers speak the Anthropic protocol through a gateway, and one
        # proxies to non-Anthropic models, so they default to no breakpoints. This
        # is a detected default, overridable per provider or per model.
        "supportsCacheControl": is_anthropic_api,
        "supportsCacheControlOnTools": True,
    }


def provider_model_max_tokens(provider: ProviderConfig, model: str | None = None) -> int | None:
    """Return the catalog output token limit for a model, or None when it is unset."""
    selected_model = model or provider.default_model
    metadata = _metadata_for_model(provider, selected_model)
    return metadata.max_tokens if metadata is not None else None


def provider_model_supports_images(provider: ProviderConfig, model: str | None = None) -> bool:
    selected_model = model or provider.default_model
    metadata = _metadata_for_model(provider, selected_model)
    return metadata is not None and "image" in metadata.input


def provider_model_compat(provider: ProviderConfig, model: str | None = None) -> dict[str, Any]:
    """Return the detected/overridden compat flags for a model (e.g. supportsStrictMode)."""
    return _model_compat(provider, model)


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
    """Pick a valid startup thinking level for a provider/model pair.

    Startup (TUI and print mode) must never crash just because the remembered
    default model does not support the global default level. The level is
    resolved with the same precedence used when switching models mid-session:
    the remembered per-model preference wins, then the global ``preferred``
    level, then the provider/catalog default, then the first available level.

    An explicit ``cli_override`` (from ``--thinking``) takes precedence over
    everything, and unlike the fallback chain it is strict: requesting a level
    the model does not support raises :class:`ProviderConfigError` instead of
    silently falling back.

    Returns ``None`` when the model has no configurable thinking levels.
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
    remembered = provider.thinking_defaults.get(model)
    if remembered in levels:
        return remembered
    if preferred in levels:
        return preferred
    return provider_default_thinking_level(provider, model=model) or levels[0]


def openai_compatible_config_from_provider(
    provider: OpenAICompatibleProviderConfig,
    *,
    credential_reader: CredentialReader | None = None,
    model: str | None = None,
    thinking_level: ThinkingLevel | None = None,
) -> OpenAICompatibleConfig:
    """Build OpenAI-compatible runtime config from durable settings."""
    api_key = _api_key_from_provider(provider, credential_reader=credential_reader)
    selected_model = model or provider.default_model
    base_url = _model_base_url(provider, selected_model)
    if provider.name == DEFAULT_PROVIDER_NAME and provider.api_key_env == "OPENAI_API_KEY":
        base_url = environ.get("OPENAI_BASE_URL", base_url)
    reasoning_effort = _reasoning_effort_from_provider(
        provider,
        model=selected_model,
        thinking_level=thinking_level,
    )
    compat = provider_model_compat(provider, selected_model)
    return OpenAICompatibleConfig(
        api_key=api_key,
        provider_name=provider.name,
        api=str(_provider_api(provider, selected_model)),
        base_url=base_url.rstrip("/"),
        headers=_model_headers(provider, selected_model),
        timeout_seconds=provider.timeout_seconds,
        max_retries=provider.max_retries,
        max_retry_delay_seconds=provider.max_retry_delay_seconds,
        supports_images=provider_model_supports_images(provider, selected_model),
        reasoning_effort=reasoning_effort,
        reasoning_effort_parameter=provider.thinking_parameter or "reasoning_effort",
        thinking_format=_thinking_format(provider, selected_model),
        compat=compat,
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
    provider: AnthropicProviderConfig,
    *,
    credential_reader: CredentialReader | None = None,
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
    base_url = _normalize_anthropic_base_url(_model_base_url(provider, selected_model))
    cache_retention, cache_control_on_tools = anthropic_cache_settings(provider, selected_model)
    return AnthropicConfig(
        api_key=api_key,
        provider_name=provider.name,
        base_url=base_url,
        cache_retention=cache_retention,
        cache_control_on_tools=cache_control_on_tools,
        headers=_model_headers(provider, selected_model),
        timeout_seconds=provider.timeout_seconds,
        max_retries=provider.max_retries,
        max_retry_delay_seconds=provider.max_retry_delay_seconds,
        max_tokens=provider_model_max_tokens(provider, selected_model),
        supports_images=provider_model_supports_images(provider, selected_model),
        thinking_budget_tokens=thinking_budget_tokens,
        thinking_effort=_reasoning_effort_from_anthropic_provider(
            provider,
            model=selected_model,
            thinking_level=thinking_level,
        ),
        thinking_mode=_anthropic_thinking_mode(
            provider, selected_model, thinking_level=thinking_level
        ),
        compat=provider_model_compat(provider, selected_model),
    )


def provider_kind(provider: ProviderConfig) -> ProviderKind:
    """Return the durable provider kind."""
    if isinstance(provider, AnthropicProviderConfig):
        return "anthropic"
    if isinstance(provider, OpenAICodexProviderConfig):
        return "openai-codex"
    if isinstance(provider, OpenAICompatibleProviderConfig):
        if provider.api == "google-generative-ai":
            return "google-generative-ai"
        if provider.api == "mistral-conversations":
            return "mistral-conversations"
    return "openai-compatible"


def provider_has_usable_credentials(
    provider: ProviderConfig,
    *,
    credential_reader: CredentialReader | None = None,
) -> bool:
    """Return whether rio can attempt calls for this provider without prompting setup."""
    if provider.credential_name and credential_reader is not None:
        get_oauth = getattr(credential_reader, "get_oauth", None)
        if (
            get_oauth_provider(provider.name) is not None
            and get_oauth is not None
            and get_oauth(provider.credential_name) is not None
        ):
            return True
        if credential_reader.get(provider.credential_name):
            return True
    return bool(environ.get(provider.api_key_env))


def _reasoning_effort_from_provider(
    provider: OpenAICompatibleProviderConfig,
    *,
    model: str | None,
    thinking_level: ThinkingLevel | None,
) -> str | None:
    if thinking_level is None or provider.thinking_parameter not in {
        "reasoning_effort",
        "reasoning.effort",
    }:
        return None

    levels = provider_thinking_levels(provider, model=model)
    if not levels:
        return None

    selected_model = model or provider.default_model
    normalized = normalize_thinking_level(thinking_level)
    if normalized not in levels:
        available = ", ".join(levels)
        raise ProviderConfigError(
            f"Thinking mode {normalized} is not available for "
            f"{provider.name}:{selected_model}. Available modes: {available}"
        )
    mapped = _metadata_thinking_value(provider, selected_model, normalized)
    if mapped is not None:
        return mapped
    if provider.name == "huggingface" and normalized == "minimal":
        # Hugging Face's router currently accepts low/medium/high/xhigh/max/none
        # for reasoning_effort, but rejects Pi/rio's "minimal" label.
        return "low"
    return reasoning_effort_for_level(normalized)


def _anthropic_thinking_budget_from_provider(
    provider: AnthropicProviderConfig,
    *,
    model: str | None,
    thinking_level: ThinkingLevel | None,
) -> int | None:
    if thinking_level is None or provider.thinking_parameter != "anthropic.thinking":
        return None

    selected_model = model or provider.default_model
    if _anthropic_thinking_mode(provider, selected_model) == "adaptive":
        return None

    levels = provider_thinking_levels(provider, model=selected_model)
    if not levels:
        return None

    normalized = normalize_thinking_level(thinking_level)
    if normalized not in levels:
        available = ", ".join(levels)
        raise ProviderConfigError(
            f"Thinking mode {normalized} is not available for "
            f"{provider.name}:{selected_model}. Available modes: {available}"
        )
    return anthropic_thinking_budget_for_level(normalized)


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
    compat = _model_compat(provider, model)
    value = compat.get("thinkingFormat")
    if isinstance(value, str) and value:
        return value
    base_url = _model_base_url(provider, model)
    if provider.name == "deepseek" or "deepseek.com" in base_url:
        return "deepseek"
    if provider.name == "zai" or "api.z.ai" in base_url:
        return "zai"
    if provider.name == "together" or "api.together.ai" in base_url:
        return "together"
    if provider.name == "openrouter" or "openrouter.ai" in base_url:
        return "openrouter"
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
    provider: AnthropicProviderConfig,
    *,
    model: str,
    thinking_level: ThinkingLevel | None,
) -> str | None:
    if thinking_level is None:
        return None
    selected_model = model
    normalized = normalize_thinking_level(thinking_level)
    if normalized == "off":
        return None
    mapped = _metadata_thinking_value(provider, selected_model, normalized)
    return mapped or normalized


def _anthropic_thinking_mode(
    provider: AnthropicProviderConfig,
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

    Capability comes from compat, which layers a detected default under the
    provider's own compat and then per-model compat. Intent comes from the auth
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
    credential_reader: CredentialReader | None,
) -> str:
    if provider.credential_name and credential_reader is not None:
        credential = credential_reader.get(provider.credential_name)
        if credential:
            return credential
        get_oauth = getattr(credential_reader, "get_oauth", None)
        if get_oauth_provider(provider.name) is not None and get_oauth is not None:
            oauth_credential = get_oauth(provider.credential_name)
            if oauth_credential is not None:
                access = getattr(oauth_credential, "access", None)
                if isinstance(access, str) and access:
                    return access

    return environ.get(provider.api_key_env, "") if provider.api_key_env else ""


def _validate_provider_numbers(
    *,
    timeout_seconds: float,
    max_retries: int,
    max_retry_delay_seconds: float,
) -> None:
    if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        raise ProviderConfigError("Provider timeout_seconds must be greater than 0")
    if not isinstance(max_retries, int) or isinstance(max_retries, bool) or max_retries < 0:
        raise ProviderConfigError("Provider max_retries must be 0 or greater")
    if (
        not isinstance(max_retry_delay_seconds, int | float)
        or isinstance(max_retry_delay_seconds, bool)
        or max_retry_delay_seconds < 0
    ):
        raise ProviderConfigError("Provider max_retry_delay_seconds must be 0 or greater")


def _validate_model_metadata(
    models: tuple[str, ...],
    model_metadata: dict[str, ProviderModelMetadata],
) -> None:
    model_names = set(models)
    for model, metadata in model_metadata.items():
        if model not in model_names:
            raise ProviderConfigError(f"Provider model_metadata key is not in models: {model}")
        if metadata.max_tokens is not None and metadata.max_tokens <= 0:
            raise ProviderConfigError("Provider model_metadata max_tokens must be positive")
        if any(item not in {"text", "image"} for item in metadata.input):
            raise ProviderConfigError("Provider model_metadata input must contain text or image")
        _validate_json_object(metadata.compat, "Provider model_metadata compat")
        _validate_string_dict(metadata.headers, "Provider model_metadata headers")
        for level, value in metadata.thinking_level_map.items():
            normalize_thinking_level(level)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ProviderConfigError(
                    "Provider model_metadata thinking_level_map values must be strings or null"
                )


def _validate_string_dict(value: dict[str, str], field_name: str) -> None:
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ProviderConfigError(f"{field_name} keys must be non-empty strings")
        if not isinstance(item, str) or not item.strip():
            raise ProviderConfigError(f"{field_name} values must be non-empty strings")


def _validate_json_object(value: dict[str, Any], field_name: str) -> None:
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ProviderConfigError(f"{field_name} keys must be non-empty strings")
        _validate_json_value(item, f"{field_name}.{key}")


def _validate_json_value(value: object, field_name: str) -> None:
    if value is None or isinstance(value, str | int | float | bool):
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, field_name)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProviderConfigError(f"{field_name} object keys must be strings")
            _validate_json_value(item, f"{field_name}.{key}")
        return
    raise ProviderConfigError(f"{field_name} must be JSON-compatible")


_HF_INFERENCE_PROVIDER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_huggingface_inference_provider(value: str) -> str:
    """Return a normalized explicit Hugging Face provider suffix."""
    normalized = value.strip()
    if not _HF_INFERENCE_PROVIDER_PATTERN.fullmatch(normalized):
        raise ProviderConfigError(
            "Hugging Face inference provider must contain only letters, numbers, '.', '_', or '-'"
        )
    if normalized in {"fastest", "cheapest", "preferred"}:
        raise ProviderConfigError(
            f"Hugging Face inference provider must be explicit, not routing policy: {normalized}"
        )
    return normalized


def _validate_inference_providers(
    provider_name: str,
    models: tuple[str, ...],
    inference_providers: dict[str, str],
) -> None:
    if inference_providers and provider_name != "huggingface":
        raise ProviderConfigError(
            "inference_providers preferences are only supported for the huggingface provider"
        )
    for model, route in inference_providers.items():
        if model not in models:
            raise ProviderConfigError(
                f"Inference-provider preference references unknown model: {model}"
            )
        validate_huggingface_inference_provider(route)


def _validate_thinking_defaults(thinking_defaults: dict[str, ThinkingLevel]) -> None:
    for model, thinking_level in thinking_defaults.items():
        if not isinstance(model, str) or not model.strip():
            raise ProviderConfigError("Provider thinking_defaults keys must be non-empty strings")
        try:
            normalize_thinking_level(thinking_level)
        except ValueError as exc:
            raise ProviderConfigError(str(exc)) from exc


def _validate_thinking_config(
    *,
    thinking_levels: tuple[ThinkingLevel, ...] | None,
    thinking_models: tuple[str, ...],
    thinking_default: ThinkingLevel | None,
    thinking_parameter: ThinkingParameter | None,
) -> None:
    if thinking_levels is None:
        if thinking_models or thinking_default is not None or thinking_parameter is not None:
            raise ProviderConfigError(
                "Provider thinking_levels must be set before thinking metadata"
            )
        return
    try:
        normalized = normalize_thinking_levels(thinking_levels)
    except ValueError as exc:
        raise ProviderConfigError(str(exc)) from exc
    if normalized != thinking_levels:
        raise ProviderConfigError("Provider thinking_levels must be normalized")
    if any(not isinstance(model, str) or not model.strip() for model in thinking_models):
        raise ProviderConfigError("Provider thinking_models must contain non-empty strings")
    if thinking_default is not None and thinking_default not in thinking_levels:
        raise ProviderConfigError("Provider thinking_default must be in thinking_levels")
    if thinking_parameter not in {
        None,
        "reasoning_effort",
        "reasoning.effort",
        "anthropic.thinking",
    }:
        raise ProviderConfigError(
            "Provider thinking_parameter must be reasoning_effort, reasoning.effort, "
            "or anthropic.thinking"
        )
