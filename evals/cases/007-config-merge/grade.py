from config import merge


def test_merge_contract() -> None:
    a = {"db": {"host": "a", "port": 1}, "items": [1], "enabled": True}
    b = {"db": {"port": 2}, "items": [], "enabled": False, "nil": None}
    out = merge(a, b)
    assert out == {"db": {"host": "a", "port": 2}, "items": [], "enabled": False, "nil": None}
    out["db"]["host"] = "changed"
    out["items"].append(2)
    assert a["db"]["host"] == "a" and b["items"] == []


def test_unmodified_nested_values_are_independent() -> None:
    defaults = {"untouched": {"items": [1]}, "count": 2, "enabled": True}
    overrides = {"count": 0, "enabled": False, "extra": [3]}
    out = merge(defaults, overrides)
    assert out["count"] == 0 and out["enabled"] is False
    out["untouched"]["items"].append(2)
    out["extra"].append(4)
    assert defaults["untouched"]["items"] == [1]
    assert overrides["extra"] == [3]
