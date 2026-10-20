import importlib
import sys
from types import SimpleNamespace

import encoder
import pytest


def test_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "fastjson", None)
    assert importlib.reload(encoder).encode({"b": 2, "a": 1}) == '{"a": 1, "b": 2}'


def test_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "fastjson", SimpleNamespace(dumps=lambda x: "fast"))
    assert importlib.reload(encoder).encode({}) == "fast"

    def broken(value: object) -> str:
        raise ValueError("bad input")

    monkeypatch.setitem(sys.modules, "fastjson", SimpleNamespace(dumps=broken))
    importlib.reload(encoder)
    with pytest.raises(ValueError):
        encoder.encode({})


def test_installed_dependency_errors_are_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    for error in (ImportError("encoder broken"), ModuleNotFoundError("missing subdependency")):

        def broken(value: object, error: Exception = error) -> str:
            raise error

        monkeypatch.setitem(sys.modules, "fastjson", SimpleNamespace(dumps=broken))
        importlib.reload(encoder)
        with pytest.raises(type(error)) as caught:
            encoder.encode({})
        assert caught.value is error
