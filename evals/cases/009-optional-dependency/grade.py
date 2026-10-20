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
