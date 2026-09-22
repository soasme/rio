"""Human-readable coding-session transcript renderer."""

from __future__ import annotations

import sys

from rio.agent import (
    ActionEndEvent,
    ActionStartEvent,
    ValidationErrorEvent,
)
from rio.ai.types import JSONObject
from rio.coding.events import (
    AutoRetryEndEvent,
    AutoRetryStartEvent,
    CodingSessionEvent,
    SessionRunEndEvent,
)

_ARGUMENT_CHARS = 200


class PlainEventRenderer:
    """Render tool activity and answers as a compact human transcript."""

    def __init__(self) -> None:
        self._failed = False
        self._error_messages: list[str] = []
        self._has_entries = False

    def render(self, event: CodingSessionEvent) -> None:
        if isinstance(event, ValidationErrorEvent):
            self._message(f"Retry: {event.error}")
        elif isinstance(event, ActionStartEvent):
            command = event.arguments.get("command")
            detail = command if isinstance(command, str) else _compact_json(event.arguments)
            self._message(f"Running {detail}" if command else f"{event.name} {detail}")
        elif isinstance(event, ActionEndEvent):
            self._result(event.name, event.result.text)
        elif isinstance(event, AutoRetryStartEvent):
            self._failed = True
            self._message(f"Retrying: {event.error_message}")
        elif isinstance(event, AutoRetryEndEvent):
            self._failed = not event.success
            if not event.success and event.final_error:
                self._error_messages.append(event.final_error)
        elif isinstance(event, SessionRunEndEvent) and event.answer:
            self._message(event.answer)

    def finish(self) -> bool:
        if self._failed:
            for message in self._error_messages:
                print(f"Error: {message}", file=sys.stderr)
            return False
        return True

    def render_message(self, text: str) -> None:
        """Render a user or assistant message as a transcript entry."""
        self._message(text)

    def _message(self, text: str) -> None:
        lines = text.splitlines() or [""]
        if self._has_entries:
            print()
        print(f"• {lines[0]}", flush=True)
        for line in lines[1:]:
            print(f"  {line}", flush=True)
        self._has_entries = True

    @staticmethod
    def _result(name: str, text: str) -> None:
        lines = text.splitlines() or [""]
        print(f"  └ {name}: {lines[0]}", flush=True)
        for line in lines[1:]:
            print(f"    {line}", flush=True)


def _compact_json(value: JSONObject, *, limit: int = _ARGUMENT_CHARS) -> str:
    import json

    text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
