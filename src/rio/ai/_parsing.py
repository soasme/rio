"""Small parsing helpers shared by provider adapters."""

from __future__ import annotations

from collections.abc import Mapping
from json import loads
from typing import Any

from rio.ai.messages import malformed_tool_arguments
from rio.ai.types import JSONValue


def parse_sse_line(line: str) -> str | None:
    """Return the payload of an SSE ``data:`` line, or ``None`` for any other line."""
    line = line.strip()
    if not line.startswith("data:"):
        return None
    return line.removeprefix("data:").strip()


def loads_object(text: str) -> dict[str, Any] | None:
    """Parse ``text`` as a JSON object, returning ``None`` for anything else."""
    try:
        value = loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def tool_arguments(arguments_text: str) -> dict[str, JSONValue]:
    """Parse streamed tool-call arguments, keeping malformed text recognizable."""
    if not arguments_text:
        return {}
    arguments = loads_object(arguments_text)
    if arguments is None:
        return malformed_tool_arguments(arguments_text)
    return arguments


def first_choice(chunk: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return the first ``choices`` entry of a chat-completions chunk."""
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0]
    return choice if isinstance(choice, Mapping) else None


def int_or_none(value: object) -> int | None:
    """Return ``value`` if it is an int (not a bool), else ``None``."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def int_or_zero(value: object) -> int:
    """Return ``value`` if it is an int (not a bool), else ``0``."""
    return int_or_none(value) or 0
