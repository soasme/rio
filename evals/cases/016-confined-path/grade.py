from pathlib import Path

import pytest
from paths import resolve_inside


def test_paths(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    assert resolve_inside(root, "a/../b") == root / "b"
    for name in ("../elsewhere", "/tmp/absolute"):
        with pytest.raises(ValueError):
            resolve_inside(root, name)
    (root / "link").symlink_to(tmp_path)
    with pytest.raises(ValueError):
        resolve_inside(root, "link/elsewhere")
