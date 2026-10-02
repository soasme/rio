"""HarnessSpec: the fixed instructions and action tools for a domain."""

from __future__ import annotations

from dataclasses import dataclass

from rio.ai.tools import AgentTool


@dataclass(frozen=True, slots=True)
class HarnessSpec:
    name: str
    instructions: str
    actions: tuple[AgentTool, ...] = ()

    def action_by_name(self) -> dict[str, AgentTool]:
        return {action.name: action for action in self.actions}
