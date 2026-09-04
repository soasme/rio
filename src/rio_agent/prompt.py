"""The per-step prompt sent to the model (arXiv:2608.26263 Appendix A.4).

The skill instructions are sent as the provider's `system` string. The
current state and the latest observation are the only other inputs -- no
past observations, actions, or reasoning traces are ever included, which is
what keeps the per-step prompt a fixed size no matter how many steps have
already run.

The model reports its output for the step -- reasoning, a state update, and
an action -- by calling a single mandatory `skill_step` tool. Any free text
or thinking before that call is the reasoning; the runtime reads it for
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
    """The one tool the model may call each step. It forces structured output: reasoning, a state update, and an action."""
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
    """Build the non-system half of the prompt: the current state and the latest observation, nothing else."""
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
