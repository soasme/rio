from datetime import datetime


def order_events(events: list[dict]) -> list[dict]:
    def key(event: dict) -> datetime:
        value = datetime.fromisoformat(event["timestamp"])
        if value.tzinfo is None:
            raise ValueError("offset required")
        return value

    return sorted(events, key=lambda e: e["timestamp"])
