"""SkillSpec: the immutable procedural specification P plus the domain's
state schema and action tools (arXiv:2608.26263 §3.1).

A skill is authored once per domain, not per task instance -- the paper's
central claim is that P and the shape of Σ stay fixed across an entire
execution horizon T while only Σ_t and the latest observation O_t vary, and
that is what keeps each step's prompt at O(|P| + |Σ| + |O|) rather than
growing with t.
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
