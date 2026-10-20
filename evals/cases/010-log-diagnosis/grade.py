import pytest
from parser import parse_status


def test_last_status() -> None:
    assert parse_status(["STATUS=old", "noise"] * 20000 + ["STATUS= ready "]) == "ready"
    assert parse_status(["xSTATUS=bad", "STATUS="]) == ""
    with pytest.raises(ValueError):
        parse_status(["noise"])
