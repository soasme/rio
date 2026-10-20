import pytest
from events import order_events


def test_offsets_and_stability() -> None:
    items = [
        {"id": 1, "timestamp": "2025-01-01T10:00:00+02:00"},
        {"id": 2, "timestamp": "2025-01-01T07:30:00Z"},
        {"id": 3, "timestamp": "2025-01-01T08:00:00Z"},
    ]
    assert [e["id"] for e in order_events(items)] == [2, 1, 3]
    assert [e["id"] for e in items] == [1, 2, 3]
    with pytest.raises(ValueError):
        order_events([{"timestamp": "2025-01-01T08:00:00"}])
