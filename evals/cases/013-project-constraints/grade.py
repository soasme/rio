from pathlib import Path

from legacy import add as legacy_add
from public_api import add


def test_api_and_scope() -> None:
    assert add(2, 3) == 5 and legacy_add(-2, -3) == -5
    assert Path("sentinel.txt").read_text() == "keep me\n"
