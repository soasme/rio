import json
from pathlib import Path

import pytest
import storage


def test_atomic_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "state.json"
    path.write_text("old")
    with pytest.raises(TypeError):
        storage.write_json(path, object())
    assert path.read_text() == "old"
    storage.write_json(path, {"name": "東京"})
    assert json.loads(path.read_text()) == {"name": "東京"}

    def fail(src: object, dst: object) -> None:
        raise OSError("disk")

    monkeypatch.setattr(storage.os, "replace", fail)
    with pytest.raises(OSError):
        storage.write_json(path, {})
    assert list(tmp_path.iterdir()) == [path]
