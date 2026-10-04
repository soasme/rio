"""Human-readable coding-session transcript renderer."""

from __future__ import annotations

import sys

from rio.ai.types import JSONObject
from rio.coding.agent import ExecutionEvent, PatchEvent, ValidationErrorEvent
from rio.coding.events import (
    AutoRetryEndEvent,
    AutoRetryStartEvent,
    CodingSessionEvent,
    SessionRunEndEvent,
)
from rio.coding.rendering.ansi import dot

_OUTPUT_LINES = 20


class PlainEventRenderer:
    """Render cell runs and answers as a compact human transcript."""

    def __init__(self) -> None:
        self._failed = False
        self._error_messages: list[str] = []
        self._has_entries = False

    def render(self, event: CodingSessionEvent) -> None:
        if isinstance(event, ValidationErrorEvent):
            self._message(f"Retry: {event.error}")
        elif isinstance(event, PatchEvent) and not event.cells and event.patch:
            self._message(f"Notebook edited ({len(event.patch)} operations)")
        elif isinstance(event, ExecutionEvent):
            for index in event.cells:
                cell = event.notebook["cells"][index]  # type: ignore[index]
                self._message(f"[{index}] {cell['source']}")  # type: ignore[index]
                self._result(cell_output_text(cell))  # type: ignore[arg-type]
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
    def _result(text: str) -> None:
        lines = text.splitlines() or ["(no output)"]
        if len(lines) > _OUTPUT_LINES:
            lines = [*lines[:_OUTPUT_LINES], f"... {len(lines) - _OUTPUT_LINES} more lines"]
        print(f"  └ {lines[0]}", flush=True)
        for line in lines[1:]:
            print(f"    {line}", flush=True)


def cell_output_text(cell: JSONObject) -> str:
    """Return a code cell's outputs as plain text."""
    parts: list[str] = []
    for output in cell.get("outputs", []):  # type: ignore[union-attr]
        kind = output.get("output_type")
        if kind == "stream":
            parts.append(_joined(output.get("text", "")))
        elif kind == "error":
            parts.append(f"{output.get('ename')}: {output.get('evalue')}")
        else:
            parts.append(_joined(output.get("data", {}).get("text/plain", "")))
    return "".join(part if part.endswith("\n") else part + "\n" for part in parts if part)


def _joined(value: object) -> str:
    return "".join(map(str, value)) if isinstance(value, list) else str(value)
