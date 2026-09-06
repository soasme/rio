"""Shared plain-text formatting for the transcript and working row."""

from rich.console import Console
from rich.text import Text

TURN_INDICATOR = "•"
RESULT_INDICATOR = "└"
DIVIDER_INDICATOR = "-"
TURN_PREFIX = f"{TURN_INDICATOR} "
RESULT_PREFIX = f"  {RESULT_INDICATOR} "


def human_elapsed(elapsed: int) -> str:
    """Format elapsed seconds, including days and hours for long runs."""
    remainder = max(0, elapsed)
    parts = []
    for seconds, suffix in ((86400, "d"), (3600, "h"), (60, "m"), (1, "s")):
        value, remainder = divmod(remainder, seconds)
        if value or parts or seconds == 1:
            parts.append(f"{value}{suffix}")
    return " ".join(parts)


def prefixed_lines(text: Text, console: Console, width: int, prefix: str) -> list[Text]:
    """Wrap before prefixing so every visual continuation retains its indent."""
    return [
        Text(prefix if index == 0 else " " * len(prefix)) + line
        for index, line in enumerate(
            text.wrap(console, max(1, width - len(prefix)), overflow="fold")
        )
    ]
