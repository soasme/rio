"""Load rio's provider catalog from packaged and user TOML files."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    ValidationError,
    model_validator,
)

from rio.ai.types import JSONValue
from rio.coding.paths import RioPaths
from rio.coding.provider_catalog import (
    ModelCatalogMetadata,
    ModelInput,
    ProviderApi,
    ProviderCatalogEntry,
    ProviderKind,
)
from rio.coding.thinking import ThinkingLevel, ThinkingParameter, normalize_thinking_levels

CATALOG_SCHEMA_VERSION = 1
USER_CATALOG_FILENAME = "catalog.toml"

# Thinking fields are merged as a group: an overlay that sets thinking_levels
# replaces all four, mirroring _merge_provider_config in provider_config.
_THINKING_FIELDS = ("thinking_levels", "thinking_models", "thinking_default", "thinking_parameter")

_NonEmptyString = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1),
]
_NonEmptyStringTuple = Annotated[tuple[_NonEmptyString, ...], Field(min_length=1)]
_PositiveInt = Annotated[StrictInt, Field(gt=0)]


class CatalogError(ValueError):
    """Raised when a rio catalog file is invalid."""


class _CatalogModelMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    api: ProviderApi | None = None
    base_url: _NonEmptyString | None = None
    reasoning: StrictBool | None = None
    input: tuple[ModelInput, ...] = ()
    max_tokens: _PositiveInt | None = None
    headers: dict[_NonEmptyString, _NonEmptyString] = {}
    compat: dict[_NonEmptyString, Any] = {}
    thinking_level_map: dict[ThinkingLevel, _NonEmptyString] = {}
    unsupported_thinking_levels: tuple[ThinkingLevel, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def ignore_informational_fields(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: item
                for key, item in value.items()
                if key not in ("name", "cost", "cost_tiers", "context_window")
            }
        return value


class _CatalogProvider(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: _NonEmptyString
    base_url: _NonEmptyString
    kind: ProviderKind = "openai-compatible"
    api_key_env: _NonEmptyString | None = None
    credential_name: _NonEmptyString | None = None
    models: _NonEmptyStringTuple | None = None
    default_model: _NonEmptyString | None = None
    api: ProviderApi | None = None
    headers: dict[_NonEmptyString, _NonEmptyString] = {}
    compat: dict[_NonEmptyString, Any] = {}
    model_metadata: dict[_NonEmptyString, _CatalogModelMetadata] = {}
    thinking_levels: tuple[ThinkingLevel, ...] | None = None
    thinking_models: tuple[_NonEmptyString, ...] = ()
    thinking_default: ThinkingLevel | None = None
    thinking_parameter: ThinkingParameter | None = None
    removed_models: tuple[_NonEmptyString, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def ignore_informational_fields(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: item
                for key, item in value.items()
                if key not in ("display_name", "docs_url", "context_windows", "auth_methods")
            }
        return value


class _CatalogFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    providers: tuple[_CatalogProvider, ...] = ()


def builtin_catalog_resource_text() -> str:
    """Return the packaged builtin catalog TOML text."""
    return files("rio.coding").joinpath("data/catalog.toml").read_text(encoding="utf-8")


@cache
def builtin_catalog() -> tuple[ProviderCatalogEntry, ...]:
    """Return rio's built-in catalog with generated models.dev model data."""
    raw = _builtin_raw_with_generated_models()
    filtered = _apply_model_tombstones(raw, base=_builtin_raw())
    return _entries_from_raw(filtered, source="built-in catalog.toml")


@cache
def builtin_source_catalog() -> tuple[ProviderCatalogEntry, ...]:
    """Return the application-owned catalog used as generator input/fallback."""
    raw = _builtin_raw()
    filtered = _apply_model_tombstones(raw, base=raw)
    return _entries_from_raw(filtered, source="built-in catalog.toml")


def user_catalog_path(paths: RioPaths | None = None) -> Path:
    """Return the user-level catalog overlay path."""
    return (paths or RioPaths()).home / USER_CATALOG_FILENAME


def effective_catalog(paths: RioPaths | None = None) -> tuple[ProviderCatalogEntry, ...]:
    """Return bundled and user-overlaid provider model data."""
    builtin_raw = _builtin_raw_with_generated_models()
    path = user_catalog_path(paths)
    if not path.exists():
        filtered = _apply_model_tombstones(builtin_raw, base=_builtin_raw())
        return _entries_from_raw(filtered, source="effective built-in catalog")
    overlay_raw = _parse_catalog_text(path.read_text(encoding="utf-8"), source=str(path))
    _validate_catalog_root(overlay_raw, source=str(path))
    merged = _merge_raw_catalogs(builtin_raw, overlay_raw)
    filtered = _apply_model_tombstones(merged, base=_builtin_raw())
    return _entries_from_raw(filtered, source=str(path))


@cache
def _builtin_raw() -> dict[str, Any]:
    return _parse_catalog_text(builtin_catalog_resource_text(), source="built-in catalog.toml")


@cache
def _builtin_raw_with_generated_models() -> dict[str, Any]:
    # Imported lazily because models_dev uses the catalog dataclasses imported by
    # this module. Missing or invalid generated data is deliberately non-fatal.
    from rio.coding.models_dev import bundled_models_dev_catalog_overlay

    raw = _builtin_raw()
    overlay = bundled_models_dev_catalog_overlay()
    if overlay is None:
        return raw
    merged = _merge_generated_catalog(raw, overlay)
    try:
        _entries_from_raw(
            _apply_model_tombstones(merged, base=raw),
            source="generated models.dev catalog",
        )
    except CatalogError:
        return raw
    return merged


def _parse_catalog_text(text: str, *, source: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise CatalogError(f"{source}: invalid TOML: {error}") from error


def _validate_catalog_root(raw: dict[str, Any], *, source: str) -> None:
    allowed = {"schema_version", "providers"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise CatalogError(f"{source}: unknown catalog keys: {', '.join(unknown)}")
    if "schema_version" not in raw:
        raise CatalogError(f"{source}: schema_version is required")
    if raw["schema_version"] != CATALOG_SCHEMA_VERSION:
        raise CatalogError(f"{source}: unsupported schema_version: {raw['schema_version']!r}")
    _raw_providers(raw)


def _merge_raw_catalogs(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge overlay provider tables over base ones; overlay values win."""
    base_providers = _raw_providers(base)
    overlay_providers = _raw_providers(overlay)
    by_name: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for provider in base_providers:
        name = _raw_provider_name(provider)
        by_name[name] = provider
        order.append(name)
    for provider in overlay_providers:
        name = _raw_provider_name(provider)
        if name in by_name:
            by_name[name] = _merge_raw_provider(by_name[name], provider)
        else:
            by_name[name] = provider
            order.append(name)
    return {
        "schema_version": overlay.get("schema_version", base.get("schema_version")),
        "providers": [by_name[name] for name in order],
    }


def _merge_generated_catalog(base: dict[str, Any], generated: dict[str, Any]) -> dict[str, Any]:
    """Replace model inventories while retaining application-owned provider config."""
    generated_by_name = {
        _raw_provider_name(provider): provider for provider in _raw_providers(generated)
    }
    providers: list[dict[str, Any]] = []
    for provider in _raw_providers(base):
        name = _raw_provider_name(provider)
        overlay = generated_by_name.get(name)
        if overlay is None:
            providers.append(provider)
            continue
        merged = _merge_raw_provider(provider, overlay)
        models = overlay.get("models")
        if not isinstance(models, list) or not models:
            providers.append(provider)
            continue
        merged["models"] = list(models)
        allowed = set(models)
        values = merged.get("model_metadata")
        if isinstance(values, dict):
            merged["model_metadata"] = {
                model: value for model, value in values.items() if model in allowed
            }
        thinking_models = merged.get("thinking_models")
        if isinstance(thinking_models, list):
            merged["thinking_models"] = [model for model in thinking_models if model in allowed]
        if merged.get("default_model") not in allowed:
            merged["default_model"] = models[0]
        providers.append(merged)
    return {"schema_version": base.get("schema_version"), "providers": providers}


def _merge_raw_provider(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = {**base, **overlay}
    base_models = base.get("models", [])
    overlay_models = overlay.get("models", [])
    if isinstance(base_models, list) and isinstance(overlay_models, list):
        merged["models"] = list(dict.fromkeys([*overlay_models, *base_models]))
    base_removed = base.get("removed_models", [])
    overlay_removed = overlay.get("removed_models", [])
    if isinstance(base_removed, list) and isinstance(overlay_removed, list):
        merged["removed_models"] = list(dict.fromkeys([*base_removed, *overlay_removed]))
    for key in ("headers", "compat"):
        base_mapping = base.get(key)
        overlay_mapping = overlay.get(key)
        if isinstance(base_mapping, dict) and isinstance(overlay_mapping, dict):
            merged[key] = {**base_mapping, **overlay_mapping}
    base_metadata = base.get("model_metadata")
    overlay_metadata = overlay.get("model_metadata")
    if isinstance(base_metadata, dict) and isinstance(overlay_metadata, dict):
        merged["model_metadata"] = _merge_model_metadata(base_metadata, overlay_metadata)
    if "thinking_levels" in overlay:
        for field in _THINKING_FIELDS:
            if field in overlay:
                merged[field] = overlay[field]
            else:
                merged.pop(field, None)
    return merged


def _apply_model_tombstones(
    raw: dict[str, Any],
    *,
    base: dict[str, Any],
) -> dict[str, Any]:
    """Remove provider-scoped models withdrawn by bundled or user catalogs."""
    base_by_name = {_raw_provider_name(provider): provider for provider in _raw_providers(base)}
    providers: list[dict[str, Any]] = []
    for provider in _raw_providers(raw):
        removed = {model for model in provider.get("removed_models", []) if isinstance(model, str)}
        if not removed:
            providers.append(provider)
            continue
        filtered = {**provider}
        for field in ("models", "thinking_models"):
            values = filtered.get(field)
            if isinstance(values, list):
                filtered[field] = [model for model in values if model not in removed]
        values = filtered.get("model_metadata")
        if isinstance(values, dict):
            filtered["model_metadata"] = {
                model: value for model, value in values.items() if model not in removed
            }
        if filtered.get("default_model") in removed:
            name = _raw_provider_name(provider)
            base_default = base_by_name.get(name, {}).get("default_model")
            remaining = filtered.get("models", [])
            filtered["default_model"] = (
                base_default
                if isinstance(base_default, str) and base_default not in removed
                else remaining[0]
                if isinstance(remaining, list) and remaining
                else ""
            )
        providers.append(filtered)
    return {**raw, "providers": providers}


def _merge_model_metadata(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {**base}
    for model, overlay_metadata in overlay.items():
        base_metadata = merged.get(model)
        if isinstance(base_metadata, dict) and isinstance(overlay_metadata, dict):
            next_metadata = {**base_metadata, **overlay_metadata}
            for key in ("headers", "compat", "thinking_level_map"):
                base_mapping = base_metadata.get(key)
                overlay_mapping = overlay_metadata.get(key)
                if isinstance(base_mapping, dict) and isinstance(overlay_mapping, dict):
                    next_metadata[key] = {**base_mapping, **overlay_mapping}
            merged[model] = next_metadata
        else:
            merged[model] = overlay_metadata
    return merged


def _raw_providers(raw: dict[str, Any]) -> list[dict[str, Any]]:
    providers = raw.get("providers", [])
    if not isinstance(providers, list) or not all(isinstance(item, dict) for item in providers):
        raise CatalogError("catalog providers must be an array of tables ([[providers]])")
    return providers


def _raw_provider_name(provider: dict[str, Any]) -> str:
    name = provider.get("name")
    if not isinstance(name, str) or not name.strip():
        raise CatalogError("catalog provider entries must have a non-empty string name")
    return name.strip()


def _entries_from_raw(raw: dict[str, Any], *, source: str) -> tuple[ProviderCatalogEntry, ...]:
    try:
        catalog = _CatalogFile.model_validate(raw)
    except ValidationError as error:
        raise CatalogError(f"{source}: {_format_validation_error(raw, error)}") from error
    entries = tuple(_entry_from_provider(provider, source=source) for provider in catalog.providers)
    names = [entry.name for entry in entries]
    if len(set(names)) != len(names):
        duplicates = sorted({name for name in names if names.count(name) > 1})
        raise CatalogError(f"{source}: duplicate provider names: {', '.join(duplicates)}")
    return entries


def _entry_from_provider(provider: _CatalogProvider, *, source: str) -> ProviderCatalogEntry:
    prefix = f"{source}: providers.{provider.name}"
    models = provider.models or (provider.default_model or provider.name,)
    default_model = provider.default_model or models[0]
    if default_model not in models:
        raise CatalogError(f"{prefix}.default_model: {provider.default_model!r} is not in models")
    if provider.thinking_levels is None:
        if provider.thinking_models or provider.thinking_parameter is not None:
            raise CatalogError(f"{prefix}.thinking_levels: required for thinking metadata")
    else:
        try:
            normalize_thinking_levels(provider.thinking_levels)
        except ValueError as error:
            raise CatalogError(f"{prefix}.thinking_levels: {error}") from error
    for model in provider.thinking_models:
        if model not in models:
            raise CatalogError(f"{prefix}.thinking_models: {model!r} is not in models")
    for model in provider.model_metadata:
        if model not in models:
            raise CatalogError(f"{prefix}.model_metadata: {model!r} is not in models")
    if provider.thinking_default is not None and (
        provider.thinking_levels is None
        or provider.thinking_default not in provider.thinking_levels
    ):
        raise CatalogError(
            f"{prefix}.thinking_default: {provider.thinking_default!r} is not in thinking_levels"
        )

    model_metadata = {
        model: _model_metadata_from_provider(metadata)
        for model, metadata in provider.model_metadata.items()
    }
    return ProviderCatalogEntry(
        name=provider.name,
        kind=provider.kind,
        base_url=provider.base_url,
        api_key_env=provider.api_key_env or "",
        credential_name=provider.credential_name,
        models=models,
        default_model=default_model,
        api=provider.api,
        headers=dict(provider.headers),
        compat=_json_object(provider.compat, f"{prefix}.compat"),
        model_metadata=model_metadata,
        thinking_levels=provider.thinking_levels,
        thinking_models=provider.thinking_models,
        thinking_default=provider.thinking_default,
        thinking_parameter=provider.thinking_parameter,
        removed_models=provider.removed_models,
    )


def _model_metadata_from_provider(metadata: _CatalogModelMetadata) -> ModelCatalogMetadata:
    thinking_level_map: dict[ThinkingLevel, str | None] = dict(metadata.thinking_level_map)
    for level in metadata.unsupported_thinking_levels:
        thinking_level_map[level] = None
    return ModelCatalogMetadata(
        api=metadata.api,
        base_url=metadata.base_url,
        reasoning=metadata.reasoning,
        input=metadata.input,
        max_tokens=metadata.max_tokens,
        headers=dict(metadata.headers),
        compat=_json_object(metadata.compat, "model_metadata.compat"),
        thinking_level_map=thinking_level_map,
    )


def _json_object(value: Mapping[str, Any], field_name: str) -> dict[str, JSONValue]:
    return {key: _json_value(item, f"{field_name}.{key}") for key, item in value.items()}


def _json_value(value: Any, field_name: str) -> JSONValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [_json_value(item, field_name) for item in value]
    if isinstance(value, dict):
        output: dict[str, JSONValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CatalogError(f"{field_name}: object keys must be strings")
            output[key] = _json_value(item, f"{field_name}.{key}")
        return output
    raise CatalogError(f"{field_name}: unsupported value {value!r}")


def _format_validation_error(raw: dict[str, Any], error: ValidationError) -> str:
    messages = []
    for issue in error.errors():
        location = ".".join(_dotted_location(raw, issue["loc"]))
        messages.append(f"{location}: {issue['msg']}")
    return "; ".join(messages)


def _dotted_location(raw: dict[str, Any], location: tuple[int | str, ...]) -> list[str]:
    parts: list[str] = []
    for part in location:
        if parts and parts[-1] == "providers" and isinstance(part, int):
            providers = raw.get("providers")
            name = None
            if isinstance(providers, list) and part < len(providers):
                item = providers[part]
                if isinstance(item, dict):
                    name = item.get("name")
            parts.append(str(name) if isinstance(name, str) else str(part))
        else:
            parts.append(str(part))
    return parts
