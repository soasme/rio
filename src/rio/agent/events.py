"""Events emitted by `rio.agent.loop.run_context_loop`.

A flat stream of small, typed records a UI or logger can subscribe to. Each
step is one model call and one action; `context` is the full list of turns the
model will see next.
"""

from __future__ import annotations

from dataclasses import dataclass

from rio.agent.context import Turn
from rio.ai.tools import AgentToolResult
from rio.ai.types import JSONObject


@dataclass(frozen=True, slots=True)
class RunStartEvent:
    skill: str


@dataclass(frozen=True, slots=True)
class StepStartEvent:
    step: int
    context: list[Turn]


@dataclass(frozen=True, slots=True)
class ReasoningEvent:
    """The text the model wrote before its action. It stays in the context."""

    step: int
    reasoning: str


@dataclass(frozen=True, slots=True)
class ValidationErrorEvent:
    """The reply had no usable action; a retry with a correction follows."""

    step: int
    attempt: int
    error: str


@dataclass(frozen=True, slots=True)
class ActionStartEvent:
    step: int
    name: str
    arguments: JSONObject


@dataclass(frozen=True, slots=True)
class ActionEndEvent:
    step: int
    name: str
    result: AgentToolResult
    is_error: bool


@dataclass(frozen=True, slots=True)
class ContextEditEvent:
    """The model edited its context file; `accepted` says whether the edit was adopted."""

    step: int
    accepted: bool
    before_tokens: int
    after_tokens: int
    turns: int


@dataclass(frozen=True, slots=True)
class StepEndEvent:
    step: int
    context: list[Turn]
    terminated: bool


@dataclass(frozen=True, slots=True)
class RunEndEvent:
    steps: int
    context: list[Turn]


type AgentEvent = (
    RunStartEvent
    | StepStartEvent
    | ReasoningEvent
    | ValidationErrorEvent
    | ActionStartEvent
    | ActionEndEvent
    | ContextEditEvent
    | StepEndEvent
    | RunEndEvent
)
