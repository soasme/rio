"""Package version helpers."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

_DISTRIBUTION_NAME = "rio"
_UNKNOWN_VERSION = "0+unknown"


def current_version() -> str:
    """Return rio's installed package version from package metadata."""
    try:
        return version(_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return _UNKNOWN_VERSION
