"""ANSI color utilities for terminal output."""

from __future__ import annotations

import os
import sys

GREEN = "\033[92m"
RED = "\033[91m"
RESET = "\033[0m"


def should_use_color() -> bool:
    """Detect if ANSI colors should be used.

    Respects NO_COLOR environment variable and checks if stdout is a TTY.
    """
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def dot(success: bool = True) -> str:
    """Get a status indicator dot, colored if appropriate.

    Green (●) for success, red (●) for failure, or middle dot (·) if colors disabled.
    """
    if not should_use_color():
        return "·"
    return f"{GREEN}●{RESET}" if success else f"{RED}●{RESET}"
