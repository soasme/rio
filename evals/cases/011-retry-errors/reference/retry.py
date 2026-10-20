from collections.abc import Callable
from typing import Any


def retry(operation: Callable, attempts: int) -> Any:
    if attempts < 1:
        raise ValueError("attempts")
    for n in range(attempts):
        try:
            return operation()
        except TimeoutError:
            if n == attempts - 1:
                raise
