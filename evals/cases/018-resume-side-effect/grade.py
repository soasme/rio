from pathlib import Path


def test_single_effect() -> None:
    assert Path("ledger.txt").read_text() == "charge-1\n"
    assert Path("receipt.txt").read_text() == "charge-1\n"
