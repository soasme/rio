"""Faithful JSON event-stream renderer for SKILL.state print mode.

Emits one JSON object per event exactly as it arrives, so a caller can pipe a
run's output through `jq` or feed it to another process. tau's
`JsonEventRenderer` only ever had Pydantic wire messages to serialize, so it
just called `model_dump_json`. Half of `CodingSessionEvent` is `rio_agent`'s
plain dataclasses instead (`StepStartEvent`, `ActionEndEvent`, ...), which
know nothing about JSON, so this module adds a small dataclass-to-JSON
bridge; the pydantic session-level events already serialize themselves.
"""

from __future__ import annotations

import json
import re
from dataclasses import fields, is_dataclass

import typer
from pydantic import BaseModel

from rio_coding.events import AutoRetryEndEvent, CodingSessionEvent

_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


class JsonEventRenderer:
    """Renders every event on the stream as one JSON line, verbatim."""

    def __init__(self) -> None:
        self._failed = False

    def render(self, event: CodingSessionEvent) -> None:
        if isinstance(event, AutoRetryEndEvent):
            self._failed = not event.success
        typer.echo(event_to_json(event))

    def finish(self) -> bool:
        return not self._failed


def event_to_json(event: CodingSessionEvent) -> str:
    """Serialize one event to a single JSON line."""
    if isinstance(event, BaseModel):
        return event.model_dump_json(by_alias=True, exclude_none=True)
    return json.dumps(_dataclass_event_payload(event), default=str)


def _dataclass_event_payload(event: object) -> dict[str, object]:
    payload: dict[str, object] = {"type": _event_type_name(event)}
    for f in fields(event):  # type: ignore[arg-type]
        payload[f.name] = _jsonable(getattr(event, f.name))
    return payload


def _jsonable(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(by_alias=True, mode="json", exclude_none=True)
    if is_dataclass(value) and not isinstance(value, type):
        return _dataclass_event_payload(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return value


def _event_type_name(event: object) -> str:
    """Return a snake_case event type tag, e.g. `StepStartEvent` -> `step_start`."""
    name = type(event).__name__.removesuffix("Event")
    return _CAMEL_BOUNDARY.sub("_", name).lower()
