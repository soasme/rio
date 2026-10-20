from __future__ import annotations

from text_utils import slugify


def test_slugify() -> None:
    assert slugify("Hello, Rio World!") == "hello-rio-world"
    assert slugify("  a___b  ") == "a-b"
    assert slugify("42 times") == "42-times"
    assert slugify("---") == ""
    assert slugify("") == ""


def test_ascii_contract() -> None:
    assert slugify("Café 東京 ABC") == "caf-abc"
    assert slugify("A\tB\nC") == "a-b-c"
