import sys
from types import SimpleNamespace

import pytest
from encoder import encode


def test_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "fastjson", None)
    assert encode({"b": 2, "a": 1}) == '{"a": 1, "b": 2}'


def test_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "fastjson", SimpleNamespace(dumps=lambda x: "fast"))
    assert encode({}) == "fast"

    def broken(value: object) -> str:
        raise ValueError("bad input")

    monkeypatch.setitem(sys.modules, "fastjson", SimpleNamespace(dumps=broken))
    with pytest.raises(ValueError):
        encode({})


def test_installed_dependency_errors_are_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    for error in (ImportError("encoder broken"), ModuleNotFoundError("missing subdependency")):

        def broken(value: object, error: Exception = error) -> str:
            raise error

        monkeypatch.setitem(sys.modules, "fastjson", SimpleNamespace(dumps=broken))
        with pytest.raises(type(error)) as caught:
            encode({})
        assert caught.value is error
