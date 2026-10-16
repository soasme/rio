"""Durable provider configuration for rio coding sessions."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field, replace
from json import JSONDecodeError, dumps, loads
from os import environ
from pathlib import Path
from shutil import copy2
from typing import Annotated, Any, Literal
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
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_RETRY_DELAY_SECONDS,
    DEFAULT_OPENAI_COMPATIBLE_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    AnthropicConfig,
    CacheRetention,
    OpenAICompatibleConfig,
)
from rio.coding.catalog_loader import builtin_catalog, effective_catalog
from rio.coding.credentials import FileCredentialStore, credentials_path
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
    reasoning_effort_for_level,
)

DEFAULT_PROVIDER_NAME = "openai"
DEFAULT_MODEL = "gpt-5.4"

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


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Provider definition validated by the catalog, with saved runtime preferences."""

    name: str
    kind: ProviderKind = "openai-compatible"
    base_url: str = DEFAULT_OPENAI_COMPATIBLE_BASE_URL
    api: ProviderApi = "openai-completions"
    api_key_env: str = "OPENAI_API_KEY"
    credential_name: str | None = None
    models: tuple[str, ...] = (DEFAULT_MODEL,)
    default_model: str = DEFAULT_MODEL
    headers: dict[str, str] = field(default_factory=dict)
    compat: dict[str, Any] = field(default_factory=dict)
    model_metadata: dict[str, ProviderModelMetadata] = field(default_factory=dict)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    max_retry_delay_seconds: float = DEFAULT_MAX_RETRY_DELAY_SECONDS
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
        kind=entry.kind,
        # Catalog anthropic entries always speak the Messages API.
        api="anthropic-messages"
        if entry.kind == "anthropic"
        else entry.api or _default_api_for_kind(entry.kind),
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
    max_retries: NonNegativeInt | None = None
    max_retry_delay_seconds: NonNegativeFloat | None = None


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


def _provider_api(provider: ProviderConfig, model: str | None = None) -> ProviderApi | str:
    selected_model = model or provider.default_model
    metadata = _metadata_for_model(provider, selected_model)
    if metadata is not None and metadata.api is not None:
        return metadata.api
    if provider.kind == "openai-codex":
        return "openai-codex-responses"
    return provider.api


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
        **provider.compat,
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
    base_url = _model_base_url(provider, selected_model)
    if provider.name == DEFAULT_PROVIDER_NAME and provider.api_key_env == "OPENAI_API_KEY":
        base_url = environ.get("OPENAI_BASE_URL", base_url)
    reasoning_effort = reasoning_effort_from_provider(
        provider,
        model=selected_model,
        thinking_level=thinking_level,
    )
    compat = _model_compat(provider, selected_model)
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
        compat=_model_compat(provider, selected_model),
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
    if provider.kind == "openai-codex":
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
    value = _model_compat(provider, model).get("thinkingFormat")
    if isinstance(value, str) and value:
        return value
    return _detected_compat(provider, model)["thinkingFormat"]


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
    selected_model = model
    normalized = normalize_thinking_level(thinking_level)
    if normalized == "off":
        return None
    mapped = _metadata_thinking_value(provider, selected_model, normalized)
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
