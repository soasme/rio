import pytest
from pages import collect


def test_falsey_cursor() -> None:
    pages = {
        None: {"items": [1], "next": 0},
        0: {"items": [1, 2], "next": ""},
        "": {"items": [], "next": None},
    }
    assert collect(pages.__getitem__) == [1, 1, 2]


def test_cycles_and_errors() -> None:
    with pytest.raises(ValueError):
        collect(lambda cursor: {"items": [], "next": "loop"})

    def fail(cursor: object) -> dict:
        raise OSError("offline")

    with pytest.raises(OSError):
        collect(fail)


def test_fetch_error_is_not_retried() -> None:
    calls = []
    error = OSError("offline")

    def fail(cursor: object) -> dict:
        calls.append(cursor)
        raise error

    with pytest.raises(OSError) as caught:
        collect(fail)
    assert caught.value is error and calls == [None]
