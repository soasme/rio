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


def test_empty_order_returns_independent_stock() -> None:
    stock = {"a": 3}
    result = checkout(stock, {})
    assert result == stock and result is not stock
    result["a"] = 0
    assert stock == {"a": 3}


def test_reserve_does_not_mutate_stock() -> None:
    stock = {"a": 3}
    result = reserve(stock, "a", 1)
    assert result == {"a": 2} and result is not stock
    assert stock == {"a": 3}
