"""Human-readable coding-session transcript renderer."""

from __future__ import annotations

import sys

from rio.agent import (
    ActionEndEvent,
    ActionStartEvent,
    ContextEditEvent,
    ValidationErrorEvent,
)
from rio.ai.tools import AgentToolResult
from rio.ai.types import JSONObject
from rio.coding.events import (
    AutoRetryEndEvent,
    AutoRetryStartEvent,
    CodingSessionEvent,
    SessionRunEndEvent,
)
from rio.coding.rendering.ansi import dot

_ARGUMENT_CHARS = 200

# These file tools echo their key argument (path, line range, diff) in their
# result, so their arguments add nothing on success -- printing them anyway
# (as `_compact_json` does for every other tool) reproduces the file content
# or the whole edited region right there in the console. Printing them stays
# useful once a call fails, since the path or edit that caused the failure is
# then exactly what a reader needs.
_FILE_TOOLS = frozenset({"read", "write", "edit"})


class PlainEventRenderer:
    """Render tool activity and answers as a compact human transcript."""

    def __init__(self) -> None:
        self._failed = False
        self._error_messages: list[str] = []
        self._has_entries = False
        self._pending_file_arguments: JSONObject | None = None

    def render(self, event: CodingSessionEvent) -> None:
        if isinstance(event, ValidationErrorEvent):
            self._message(f"Retry: {event.error}")
        elif isinstance(event, ActionStartEvent):
            if event.name in _FILE_TOOLS:
                self._pending_file_arguments = event.arguments
                return
            command = event.arguments.get("command")
            detail = command if isinstance(command, str) else _compact_json(event.arguments)
            self._message(f"Running {detail}" if command else f"{event.name} {detail}")
        elif isinstance(event, ActionEndEvent):
            if event.name in _FILE_TOOLS:
                self._file_tool_result(event)
            else:
                self._result(event.name, event.result.text)
        elif isinstance(event, ContextEditEvent):
            outcome = "edited" if event.accepted else "edit rejected"
            self._message(
                f"Context {outcome}: ~{event.before_tokens} -> ~{event.after_tokens} tokens"
            )
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

    @staticmethod
    def _result(name: str, text: str) -> None:
        lines = text.splitlines() or [""]
        print(f"  └ {name}: {lines[0]}", flush=True)
        for line in lines[1:]:
            print(f"    {line}", flush=True)

    def _file_tool_result(self, event: ActionEndEvent) -> None:
        arguments = self._pending_file_arguments or {}
        self._pending_file_arguments = None
        if event.is_error:
            self._message(f"{event.name} {_compact_json(arguments)}")
            self._result(event.name, event.result.text)
            return
        self._message(_file_tool_summary(event.name, event.result))


def _file_tool_summary(name: str, result: AgentToolResult) -> str:
    """Build a one-line success summary for a file tool, e.g. `read a.py:1:40`.

    Unlike `_result`, this never echoes file content: a read shows the line
    range it read, a write shows only the path, and an edit shows its unified
    diff (already computed by the tool) instead of a generic "replaced N
    block(s)" message.
    """
    details = result.details if isinstance(result.details, dict) else {}
    path = details.get("path")
    header = f"{name} {path}" if isinstance(path, str) else name
    if name == "read":
        start, end = details.get("start_line"), details.get("end_line")
        if isinstance(start, int) and isinstance(end, int):
            return f"read {path}:{start}:{end}"
        return header
    if name == "edit":
        patch = details.get("patch")
        if isinstance(patch, str) and patch.strip():
            return f"{header}\n{patch.rstrip()}"
        return header
    return header


def _compact_json(value: JSONObject, *, limit: int = _ARGUMENT_CHARS) -> str:
    import json

    text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
