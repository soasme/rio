from __future__ import annotations

import contextlib
from collections.abc import Iterator

import pytest

from rio.coding import catalog_loader, models_dev, models_dev_store, provider_catalog

_REAL_BUNDLED_LOADER = models_dev.bundled_models_dev_catalog_document


def _clear_catalog_caches() -> None:
    catalog_loader.builtin_catalog.cache_clear()
    catalog_loader._builtin_raw_with_generated_models.cache_clear()
    provider_catalog._load_builtin_catalog.cache_clear()


@contextlib.contextmanager
def _bundled_models_dev_catalog(enabled: bool) -> Iterator[None]:
    loader = _REAL_BUNDLED_LOADER if enabled else lambda: None
    with pytest.MonkeyPatch.context() as monkeypatch:
        for module in (models_dev, models_dev_store):
            monkeypatch.setattr(module, "bundled_models_dev_catalog_document", loader)
        _clear_catalog_caches()
        try:
            yield
        finally:
            _clear_catalog_caches()


@pytest.fixture(scope="session", autouse=True)
def _without_bundled_models_dev_catalog() -> Iterator[None]:
    """Keep the regenerated models.dev snapshot out of tests.

    The snapshot mirrors upstream data that changes on every refresh, so tests
    see only the repo-owned catalog.toml. Tests of the snapshot itself use the
    `bundled_models_dev_catalog` fixture and check its schema, not its values.
    """
    with _bundled_models_dev_catalog(enabled=False):
        yield


@pytest.fixture
def bundled_models_dev_catalog() -> Iterator[None]:
    """Load the real bundled models.dev snapshot for this test."""
    with _bundled_models_dev_catalog(enabled=True):
        yield
