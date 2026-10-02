"""The coding domain expressed as a CLM skill: fixed instructions plus action tools.

The instructions are built in `rio.coding.system_prompt`. Everything that
varies between steps lives in the context, which the model manages itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from rio.agent import HarnessSpec
from rio.ai.tools import AgentTool

RESPOND_ACTION = "respond"


@dataclass(frozen=True, slots=True)
class CodingSkillOptions:
    """Inputs that vary per session but not per step."""

    instructions: str
    tools: tuple[AgentTool, ...] = ()
    name: str = "rio-coding"


def build_coding_skill(options: CodingSkillOptions) -> HarnessSpec:
    """Return the `HarnessSpec` that drives a coding session."""
    return HarnessSpec(
        name=options.name, instructions=options.instructions, actions=tuple(options.tools)
    )
