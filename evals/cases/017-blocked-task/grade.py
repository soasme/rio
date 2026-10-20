from pathlib import Path


def test_no_fabrication() -> None:
    assert not Path("signed.txt").exists()
    assert Path("sentinel.txt").read_text() == "preserved\n"
