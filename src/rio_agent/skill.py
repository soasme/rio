"""HarnessSpec: the fixed instructions for a domain, plus its state schema and
action tools.

You write a skill once per domain, not once per task. The instructions and
the shape of the state stay the same for the whole run. Only the state
values and the latest observation change from step to step. That is why
each step's prompt stays a fixed size instead of growing as the run goes on.

A skill may also supply an `ActionObserver`. The model authors its own state
update before it knows what its action returned, so anything only the runtime
can know -- whether the action succeeded, what a file actually contains -- has
to be written after the fact. The observer is that seam: it may reject an
action before it runs, and it records what the action produced into the state
once it has.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from rio_ai.tools import AgentTool, AgentToolResult
from rio_ai.types import JSONObject, JSONValue


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    """What the runtime itself records about an action the model just took.

    `delta` is merged into the state exactly like a model-authored one.
    `note` is appended to the next observation, which is the only channel
    that reaches the model without passing through the state.
    """

    delta: JSONObject = field(default_factory=dict)
    note: str | None = None


@runtime_checkable
class ActionObserver(Protocol):
    """Runtime-owned state around an action, on either side of running it."""

    def before_action(
        self, state: JSONObject, name: str, arguments: Mapping[str, JSONValue]
    ) -> None:
        """Raise to refuse the action. The exception becomes the step's observation."""
        ...

    def after_action(
        self,
        state: JSONObject,
        name: str,
        arguments: Mapping[str, JSONValue],
        result: AgentToolResult,
    ) -> ActionOutcome:
        """Return what the runtime records about a successful action."""
        ...


@dataclass(frozen=True, slots=True)
class HarnessSpec:
    name: str
    instructions: str
    state_fields: tuple[str, ...]
    initial_state: JSONObject = field(default_factory=dict)
    actions: tuple[AgentTool, ...] = ()
    #: Largest state the model may build, in characters of serialized JSON.
    #: The state is sent in full every step, so an unbounded state is an
    #: unbounded prompt. `None` leaves the size unchecked.
    state_budget_chars: int | None = None
    observer: ActionObserver | None = None

    def action_by_name(self) -> dict[str, AgentTool]:
        return {action.name: action for action in self.actions}
