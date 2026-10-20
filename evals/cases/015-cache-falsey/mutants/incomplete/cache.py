from collections.abc import Callable
from typing import Any


def get_or_load(cache: dict, key: str, loader: Callable) -> Any:
    if not cache.get(key):
        cache[key] = loader(key)
    return cache[key]
