"""SkillSpec: the fixed instructions for a domain, plus its state schema and
action tools.

You write a skill once per domain, not once per task. The instructions and
the shape of the state stay the same for the whole run. Only the state
values and the latest observation change from step to step. That is why
each step's prompt stays a fixed size instead of growing as the run goes on.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rio_ai.tools import AgentTool
from rio_ai.types import JSONObject


@dataclass(frozen=True, slots=True)
class SkillSpec:
    name: str
    instructions: str
    state_fields: tuple[str, ...]
    initial_state: JSONObject = field(default_factory=dict)
    actions: tuple[AgentTool, ...] = ()

    def action_by_name(self) -> dict[str, AgentTool]:
        return {action.name: action for action in self.actions}
