"""The per-step prompt A_t = (P, Σ_t, O_t) (arXiv:2608.26263 Appendix A.4).

P is sent as the provider `system` string. Σ_t and O_t are the only other
inputs -- no historical observations, actions, or reasoning traces are ever
included, which is what bounds the per-step prompt size independently of
how many steps have already run.

The model reports its step output -- (R_t reasoning, ΔΣ_t state update, a_t
action) -- by calling a single mandatory `skill_step` tool. Free-text/
thinking content preceding that call is R_t; the runtime reads it for
observability then discards it forever (see `rio_agent.loop`).
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from rio_agent.skill import SkillSpec
from rio_ai.messages import AgentMessage, UserMessage
from rio_ai.tools import AgentTool, AgentToolResult, ToolCancellationToken, ToolUpdateCallback
from rio_ai.types import JSONObject, JSONValue

STEP_TOOL_NAME = "skill_step"


async def _skill_step_not_executed(
    tool_call_id: str,
    arguments: Mapping[str, JSONValue],
    signal: ToolCancellationToken | None = None,
    on_update: ToolUpdateCallback | None = None,
) -> AgentToolResult:
    raise RuntimeError(
        f"{STEP_TOOL_NAME!r} is intercepted and validated by the SKILL.state loop; "
        "it is never executed as an ordinary tool."
    )


def skill_step_tool(skill: SkillSpec) -> AgentTool:
    """The one tool the model may call each step, forcing structured (R_t, ΔΣ_t, a_t) output."""
    parameters: JSONObject = {
        "type": "object",
        "properties": {
            "reasoning": {
                "type": "string",
                "description": (
                    "Private scratch reasoning. Never shown back to you in a future "
                    "step -- anything that must persist belongs in state_delta instead."
                ),
            },
            "state_delta": {
                "type": "object",
                "description": (
                    "Partial update merged into the execution state (JSON Merge Patch, "
                    "RFC 7396): set a field to null to delete it, an object to merge "
                    "recursively, anything else to replace it. Only declared fields: "
                    + ", ".join(skill.state_fields)
                ),
            },
            "action": {
                "type": "object",
                "description": "The single action to execute this step.",
                "properties": {
                    "name": {"type": "string", "enum": list(skill.action_by_name())},
                    "arguments": {"type": "object"},
                },
                "required": ["name", "arguments"],
            },
        },
        "required": ["state_delta", "action"],
    }
    return AgentTool(
        name=STEP_TOOL_NAME,
        label="Skill Step",
        description="Advance the skill by one step: reason privately, update state, act.",
        parameters=parameters,
        execute_fn=_skill_step_not_executed,
    )


def build_step_messages(
    state: JSONObject,
    observation: str,
    *,
    error_note: str | None = None,
) -> list[AgentMessage]:
    """Build A_t's non-system half: exactly Σ_t and O_t, nothing else."""
    body = (
        "Skill Execution State:\n```json\n"
        + json.dumps(state, indent=2, sort_keys=True)
        + "\n```\n\nLatest Observation:\n"
        + observation
    )
    if error_note:
        body += (
            f"\n\nYour previous {STEP_TOOL_NAME} call was rejected: {error_note}\n"
            "Retry with a corrected call."
        )
    return [UserMessage(content=body)]
