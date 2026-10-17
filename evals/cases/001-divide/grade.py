from __future__ import annotations

import pytest
from calculator import divide


def test_division() -> None:
    assert divide(7, 2) == 3.5
    assert divide(-7, 2) == -3.5
    assert divide(0, 2) == 0


def test_zero_division() -> None:
    with pytest.raises(ZeroDivisionError):
        divide(1, 0)
