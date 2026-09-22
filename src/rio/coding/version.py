"""Deprecated compatibility exports for package version helpers."""

from __future__ import annotations

import warnings

from rio.version import current_version

warnings.warn(
    "rio.coding.version is deprecated; use rio.version instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["current_version"]
