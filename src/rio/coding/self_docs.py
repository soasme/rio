"""Locations of rio's packaged self-documentation and examples."""

from __future__ import annotations

from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent
_DATA_ROOT = _PACKAGE_ROOT / "data"


def rio_readme_path() -> Path:
    """Return the installed overview document for rio-aware tasks."""
    return _DATA_ROOT / "docs" / "README.md"


def rio_docs_path() -> Path:
    """Return the installed rio self-documentation directory."""
    return _DATA_ROOT / "docs"


def rio_examples_path() -> Path:
    """Return the installed rio example directory."""
    return _DATA_ROOT / "examples"
