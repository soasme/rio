import pytest
from cache import get_or_load


def test_falsey_and_errors() -> None:
    def forbidden(key: str) -> None:
        raise AssertionError("called")

    for value in (None, False, 0, ""):
        assert get_or_load({"x": value}, "x", forbidden) == value
    cache = {}
    with pytest.raises(AssertionError):
        get_or_load(cache, "x", forbidden)
    assert cache == {}
    assert get_or_load(cache, "x", lambda k: 2) == 2 and cache == {"x": 2}
