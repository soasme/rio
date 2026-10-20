from collections.abc import Callable


def collect(fetch: Callable) -> list:
    cursor = None
    seen = set()
    result = []
    while True:
        page = fetch(cursor)
        result.extend(page["items"])
        cursor = page["next"]
        if cursor is None:
            return result
        if cursor in seen:
            raise ValueError("cursor cycle")
        seen.add(cursor)
