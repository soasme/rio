from config import merge


def test_merge_contract() -> None:
    a = {"db": {"host": "a", "port": 1}, "items": [1], "enabled": True}
    b = {"db": {"port": 2}, "items": [], "enabled": False, "nil": None}
    out = merge(a, b)
    assert out == {"db": {"host": "a", "port": 2}, "items": [], "enabled": False, "nil": None}
    out["db"]["host"] = "changed"
    out["items"].append(2)
    assert a["db"]["host"] == "a" and b["items"] == []
