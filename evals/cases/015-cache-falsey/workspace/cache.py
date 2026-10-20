from collections.abc import Callable
from typing import Any


def get_or_load(cache: dict, key: str, loader: Callable) -> Any:
    raise NotImplementedError()
