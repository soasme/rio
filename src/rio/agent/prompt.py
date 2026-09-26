"""The append-only per-step prompt sent to the model.

The runtime owns the materialized execution state, but the model sees its
history: the initial state, every accepted state patch, and every observation.
This keeps state construction deterministic while preserving the ordinary
agent property that later decisions can inspect earlier evidence.

The model reports its output for the step -- reasoning, a state update, and
an action -- by calling a single mandatory `skill_step` tool. Any free text
or thinking before that call is the reasoning; the runtime reads it for
observability then discards it forever (see `rio.agent.loop`).
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from rio.agent.skill import HarnessSpec
from rio.ai.messages import AgentMessage, UserMessage
from rio.ai.tools import AgentTool, AgentToolResult, ToolCancellationToken, ToolUpdateCallback
from rio.ai.types import JSONObject, JSONValue

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


def skill_step_tool(skill: HarnessSpec) -> AgentTool:
    """The one tool the model may call each step. It forces structured output: reasoning, a state
    update, and an action.
    """
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


def state_message(
    state: JSONObject, *, rebuilt: bool = False, needs_refresh: bool = False
) -> UserMessage:
    """Return the history record that establishes a state baseline."""
    label = "State rebuild" if rebuilt else "Initial state"
    content = f"{label}:\n```json\n{json.dumps(state, indent=2, sort_keys=True)}\n```"
    if needs_refresh:
        content += (
            "\nThis state still exceeds the context target. "
            "Remove nonessential fields in state_delta."
        )
    return UserMessage(content=content)


def state_patch_message(delta: JSONObject) -> UserMessage:
    """Return an accepted RFC 7396 patch as an immutable history record."""
    return UserMessage(
        content=f"State patch:\n```json\n{json.dumps(delta, indent=2, sort_keys=True)}\n```"
    )


def observation_message(observation: str) -> UserMessage:
    """Return an action result as an immutable history record."""
    return UserMessage(content="Observation:\n" + observation)


def build_step_messages(
    history: list[AgentMessage], *, error_note: str | None = None
) -> list[AgentMessage]:
    """Return the complete history, plus a transient validation correction."""
    messages = list(history)
    if error_note:
        messages.append(
            UserMessage(
                content=(
                    f"Rejected {STEP_TOOL_NAME} call: {error_note}\n"
                    "Retry with a corrected call."
                )
            )
        )
    return messages
