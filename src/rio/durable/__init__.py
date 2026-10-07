"""Durable computation at immutable Cell boundaries.

Keep imports lazy so ``rio.durable.api`` works inside dependency-free uv scripts.
"""

from importlib import import_module

_EXPORTS = {
    "InvalidPatch": "state",
    "Limits": "state",
    "Session": "session",
    "StorageFailure": "store",
    "Store": "store",
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    return getattr(import_module(f"rio.durable.{_EXPORTS[name]}"), name)
