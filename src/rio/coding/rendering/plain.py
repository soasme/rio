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
from rio.coding.rendering.ansi import dot

_ARGUMENT_CHARS = 200
_DISPLAY_MAX_LINES = 3
_EXIT_STATUS_PREFIX = "Command exited with code "


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
            self._render_result(event)
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
        indicator = dot(success=not self._failed)
        print(f"{indicator} {lines[0]}", flush=True)
        for line in lines[1:]:
            print(f"  {line}", flush=True)
        self._has_entries = True

    def _render_result(self, event: ActionEndEvent) -> None:
        """Print a tool result, deriving a bash pass/fail dot from its details.

        `event.result.text` is also the model's next observation (see
        `rio.coding.tools`), so it must stay untouched -- this method only
        reshapes a local copy for the human-facing transcript: dropping the
        `bash <command> (...)` header (the start line already named the
        command), the redundant "Command exited with code N" line (the dot
        conveys that), and capping the displayed body to a few lines.
        """
        text = event.result.text
        success: bool | None = None
        if event.name == "bash":
            details = event.result.details if isinstance(event.result.details, dict) else {}
            success = (
                details.get("exit_code") == 0
                and not details.get("timed_out")
                and not details.get("cancelled")
            )
            text = _strip_bash_header(text)
            text = _strip_exit_status_suffix(text)
            if text.strip() == "(no output)":
                text = ""
            text = _cap_display_lines(text)
        self._result(event.name, text, success=success)

    @staticmethod
    def _result(name: str, text: str, success: bool | None = None) -> None:
        indicator = f"{dot(success=success)} " if success is not None else ""
        lines = text.splitlines() or [""]
        first = lines[0]
        suffix = f": {first}" if first else ""
        print(f"  └ {indicator}{name}{suffix}", flush=True)
        for line in lines[1:]:
            print(f"    {line}", flush=True)


def _strip_bash_header(text: str) -> str:
    """Drop the `bash <command> (...)` header; the start line already named the command."""
    _header, sep, rest = text.partition("\n\n")
    return rest if sep else text


def _strip_exit_status_suffix(text: str) -> str:
    """Drop the trailing generic exit-code line; the color dot already conveys it."""
    body, sep, tail = text.rpartition("\n\n")
    if sep and tail.startswith(_EXIT_STATUS_PREFIX):
        return body
    return text


def _cap_display_lines(text: str, *, max_lines: int = _DISPLAY_MAX_LINES) -> str:
    """Cap displayed output to `max_lines`, noting how many lines were hidden."""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    remaining = len(lines) - max_lines
    shown = "\n".join(lines[:max_lines])
    noun = "line" if remaining == 1 else "lines"
    return f"{shown}\n+{remaining} more {noun}"


def _compact_json(value: JSONObject, *, limit: int = _ARGUMENT_CHARS) -> str:
    import json

    text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
