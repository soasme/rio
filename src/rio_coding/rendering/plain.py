"""Plain-text per-step renderer for SKILL.state print mode.

tau's `FinalTextRenderer` only ever printed one thing -- the final assistant
message -- because a separate live transcript renderer had already shown
everything leading up to it. rio has no such second renderer standing next
to this one (see `rio_coding.rendering.base.PrintOutputMode`), so this is the
only live view of a run: for every committed step it prints the action
taken, the state delta the model proposed (the most informative thing in a
SKILL.state run -- it is literally what the model just learned), and a
truncated observation. Discarded reasoning and validation retries are
rendered too, but visibly marked, since a reader must never mistake either
for something that is part of the session's durable memory.
"""

from __future__ import annotations

import json

import typer

from rio_agent import (
    ActionEndEvent,
    ActionStartEvent,
    ReasoningDiscardedEvent,
    RunEndEvent,
    RunStartEvent,
    StateUpdateEvent,
    StepEndEvent,
    StepStartEvent,
    ValidationErrorEvent,
)
from rio_ai.types import JSONObject
from rio_coding.events import (
    AutoRetryEndEvent,
    AutoRetryStartEvent,
    CodingSessionEvent,
    SessionRunEndEvent,
)

_OBSERVATION_CHARS = 400
_ARGUMENT_CHARS = 200


class PlainEventRenderer:
    """Renders each SKILL.state step as it commits, in plain text."""

    def __init__(self) -> None:
        self._failed = False
        self._error_messages: list[str] = []

    def render(self, event: CodingSessionEvent) -> None:
        if isinstance(event, RunStartEvent):
            typer.echo(f"== {event.skill} ==")
        elif isinstance(event, StepStartEvent):
            typer.echo(f"-- step {event.step} --")
        elif isinstance(event, ReasoningDiscardedEvent):
            # Shown once, for observability only. It is never fed back to the
            # model, so the label must make that unmistakable.
            typer.echo(f"[reasoning, discarded] {_truncate(event.reasoning)}")
        elif isinstance(event, ValidationErrorEvent):
            typer.echo(f"[retry {event.attempt}] {event.error}")
        elif isinstance(event, StateUpdateEvent):
            typer.echo(f"state delta: {_compact_json(event.delta)}")
        elif isinstance(event, ActionStartEvent):
            typer.echo(f"-> {event.name}({_compact_json(event.arguments)})")
        elif isinstance(event, ActionEndEvent):
            status = "x" if event.is_error else "ok"
            typer.echo(f"<- [{status}] {event.name}: {_truncate(event.result.text)}")
        elif isinstance(event, StepEndEvent):
            if event.terminated:
                typer.echo("(terminated)")
        elif isinstance(event, RunEndEvent):
            typer.echo(f"== {event.steps} step(s) ==")
        elif isinstance(event, AutoRetryStartEvent):
            self._failed = True
            typer.echo(f"... {event.error_message}")
        elif isinstance(event, AutoRetryEndEvent):
            self._failed = not event.success
            if not event.success and event.final_error:
                self._error_messages.append(event.final_error)
        elif isinstance(event, SessionRunEndEvent) and event.answer:
            typer.echo(event.answer)

    def finish(self) -> bool:
        if self._failed:
            for message in self._error_messages:
                typer.echo(f"Error: {message}", err=True)
            return False
        return True


def _truncate(text: str, *, limit: int = _OBSERVATION_CHARS) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."


def _compact_json(value: JSONObject, *, limit: int = _ARGUMENT_CHARS) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
