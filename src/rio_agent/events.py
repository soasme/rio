"""Events emitted by `rio_agent.loop.run_skill_loop`.

Shaped like `rio_ai`'s ported tau_agent-style agent events (a flat stream of
small, typed records a UI or logger can subscribe to) but describing SKILL.
state's step lifecycle instead of an append-only conversation turn.
"""

from __future__ import annotations

from dataclasses import dataclass

from rio_ai.tools import AgentToolResult
from rio_ai.types import JSONObject


@dataclass(frozen=True, slots=True)
class RunStartEvent:
    skill: str


@dataclass(frozen=True, slots=True)
class StepStartEvent:
    step: int
    state: JSONObject
    observation: str


@dataclass(frozen=True, slots=True)
class ReasoningDiscardedEvent:
    """The model's reasoning for this step. Surfaced once for observability, then never sent back
    to the model.
    """

    step: int
    reasoning: str


@dataclass(frozen=True, slots=True)
class ValidationErrorEvent:
    """A proposed state update or action failed runtime validation; a rollback-retry follows."""

    step: int
    attempt: int
    error: str


@dataclass(frozen=True, slots=True)
class StateUpdateEvent:
    """The proposed state update was validated and committed to form the new state."""

    step: int
    delta: JSONObject
    state: JSONObject


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
class StepEndEvent:
    step: int
    state: JSONObject
    terminated: bool


@dataclass(frozen=True, slots=True)
class RunEndEvent:
    steps: int
    state: JSONObject


type SkillEvent = (
    RunStartEvent
    | StepStartEvent
    | ReasoningDiscardedEvent
    | ValidationErrorEvent
    | StateUpdateEvent
    | ActionStartEvent
    | ActionEndEvent
    | StepEndEvent
    | RunEndEvent
)
