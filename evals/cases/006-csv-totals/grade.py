from decimal import Decimal

import pytest
from totals import totals


def test_quoted_and_exact() -> None:
    assert totals('customer,amount\n"A, Inc",0.1\n"A, Inc",0.2\nB,-2\n') == {
        "A, Inc": Decimal("0.3"),
        "B": Decimal("-2"),
    }


def test_empty_and_invalid() -> None:
    assert totals("") == {}
    assert totals("customer,amount\n") == {}
    with pytest.raises(ValueError):
        totals("customer,amount\nA,bad\n")


def test_precision() -> None:
    assert totals("customer,amount\nA,9007199254740993.01\n")["A"] == Decimal("9007199254740993.01")
