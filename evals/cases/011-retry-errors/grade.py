import pytest
from retry import retry


def test_transient_and_falsey() -> None:
    calls = []

    def op() -> int:
        calls.append(1)
        if len(calls) < 3:
            raise TimeoutError("retry")
        return 0

    assert retry(op, 3) == 0 and len(calls) == 3


def test_no_extra_attempts() -> None:
    error = TimeoutError("last")
    calls = []

    def op() -> None:
        calls.append(1)
        raise error

    with pytest.raises(TimeoutError) as caught:
        retry(op, 2)
    assert caught.value is error and len(calls) == 2
    with pytest.raises(ValueError):
        retry(op, 0)

    def fatal() -> None:
        raise KeyError("fatal")

    with pytest.raises(KeyError):
        retry(fatal, 3)
