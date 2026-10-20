from collections.abc import Callable
from typing import Any


def retry(operation: Callable, attempts: int) -> Any:
    raise NotImplementedError()
