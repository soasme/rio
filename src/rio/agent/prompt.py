"""The default JSON Patch protocol."""

from __future__ import annotations

import json
from collections.abc import Mapping

from rio.ai.messages import AgentMessage, UserMessage
from rio.ai.tools import AgentTool, AgentToolResult, ToolCancellationToken, ToolUpdateCallback
from rio.ai.types import JSONObject, JSONValue

STEP_TOOL_NAME = "skill_step"
DEFAULT_SYSTEM_PROMPT = (
    "Your context is a JSON object sent in full each step. Call `skill_step` with "
    "an RFC 6902 JSON Patch against that object. Use the patch to update your "
    "state. Set `reply` when you have an answer and want to end the run."
)


async def _not_executed(
    tool_call_id: str,
    arguments: Mapping[str, JSONValue],
    signal: ToolCancellationToken | None = None,
    on_update: ToolUpdateCallback | None = None,
) -> AgentToolResult:
    raise RuntimeError("skill_step is applied by the agent loop")


def skill_step_tool() -> AgentTool:
    parameters: JSONObject = {
        "type": "object",
        "properties": {
            "patch": {
                "type": "array",
                "description": "RFC 6902 JSON Patch operations applied to the state.",
                "items": {"type": "object"},
            },
            "reply": {"type": "string", "description": "Answer and end the run."},
        },
        "required": ["patch"],
    }
    return AgentTool(
        name=STEP_TOOL_NAME,
        label="Skill Step",
        description="Patch the JSON state.",
        parameters=parameters,
        execute_fn=_not_executed,
    )


def build_messages(state: JSONObject, *, error_note: str | None = None) -> list[AgentMessage]:
    text = f"```json\n{json.dumps(state, ensure_ascii=False)}\n```"
    if error_note:
        text += f"\n\nRejected reply: {error_note}\nRetry with a corrected reply."
    return [UserMessage(content=text)]
