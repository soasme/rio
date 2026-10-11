"""Read the persisted models.dev catalog cache."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from rio.coding.models_dev import (
    bundled_models_dev_catalog_document,
    models_dev_catalog_overlay,
)
from rio.coding.paths import RioPaths

MODELS_STORE_SCHEMA_VERSION = 1


def models_store_path(paths: RioPaths | None = None) -> Path:
    return (paths or RioPaths()).models_store_path


def cached_models_dev_catalog_document(
    paths: RioPaths | None = None,
) -> dict[str, Any] | None:
    """Return a valid cache only when it is newer than the bundled snapshot."""
    cache = _read_cache(paths)
    if cache is None:
        return None
    bundled = bundled_models_dev_catalog_document() or {}
    bundled_at = bundled.get("generated_at")
    cached_at = cache["catalog"].get("generated_at")
    if isinstance(bundled_at, int) and isinstance(cached_at, int) and cached_at <= bundled_at:
        return None
    return cast(dict[str, Any], cache["catalog"])


def cached_models_dev_catalog_overlay(paths: RioPaths | None = None) -> dict[str, Any] | None:
    document = cached_models_dev_catalog_document(paths)
    return models_dev_catalog_overlay(document) if document is not None else None


def _read_cache(paths: RioPaths | None) -> dict[str, Any] | None:
    path = models_store_path(paths)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != MODELS_STORE_SCHEMA_VERSION
        ):
            return None
        checked_at = value.get("checked_at")
        catalog = value.get("catalog")
        if not isinstance(checked_at, int | float) or not isinstance(catalog, dict):
            return None
        models_dev_catalog_overlay(catalog)
        return value
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
