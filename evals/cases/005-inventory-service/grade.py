import pytest
from inventory import reserve
from service import checkout


def test_atomic_order() -> None:
    stock = {"a": 3, "b": 1}
    assert checkout(stock, {"a": 2, "b": 1}) == {"a": 1, "b": 0}
    with pytest.raises(ValueError):
        checkout(stock, {"a": 2, "b": 2})
    assert stock == {"a": 3, "b": 1}


def test_validation() -> None:
    for n in (0, -1, 4):
        with pytest.raises(ValueError):
            reserve({"a": 3}, "a", n)
    with pytest.raises(KeyError):
        reserve({}, "missing", 1)
