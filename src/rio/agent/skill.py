"""HarnessSpec: the fixed instructions for a domain, plus its state schema and
action tools.

You write a skill once per domain, not once per task. The instructions and
the shape of the state stay the same for the whole run. Only the state
values and the latest observation change from step to step. That is why
each step's prompt stays a fixed size instead of growing as the run goes on.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from rio.ai.tools import AgentTool, AgentToolResult, ToolCancellationToken
from rio.ai.types import JSONObject, JSONValue


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
    # An executor returns the normal tool result and a runtime state delta.
    execute_action: (
        Callable[
            [AgentTool, str, Mapping[str, JSONValue], JSONObject, ToolCancellationToken | None],
            Awaitable[tuple[AgentToolResult, JSONObject]],
        ]
        | None
    ) = None

    def action_by_name(self) -> dict[str, AgentTool]:
        return {action.name: action for action in self.actions}
