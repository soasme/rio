from __future__ import annotations

import itertools
from collections.abc import Mapping

from rio.agent import HarnessSpec
from rio.ai import (
    AgentTool,
    AgentToolResult,
    AssistantDoneEvent,
    AssistantMessage,
    TextContent,
    ToolCall,
)

_call_ids = itertools.count()


def step_response(*, reasoning: str = "", action: str, args: Mapping | None = None):
    """Build one FakeProvider stream: reasoning text and a single tool call."""
    content = []
    if reasoning:
        content.append(TextContent(text=reasoning))
    content.append(ToolCall(id=f"call-{next(_call_ids)}", name=action, arguments=dict(args or {})))
    message = AssistantMessage(content=content, stop_reason="toolUse")
    return [AssistantDoneEvent(reason="toolUse", message=message)]


async def _advance(tool_call_id, arguments, signal=None, on_update=None):
    note = arguments.get("note", "")
    return AgentToolResult(content=[TextContent(text=f"observed:{note}")])


async def _finish(tool_call_id, arguments, signal=None, on_update=None):
    return AgentToolResult(content=[TextContent(text="terminal")], terminate=True)


ADVANCE = AgentTool(
    name="advance",
    label="Advance",
    description="Advance the task by one step without ending the run.",
    parameters={"type": "object", "properties": {"note": {"type": "string"}}},
    execute_fn=_advance,
)
FINISH = AgentTool(
    name="finish",
    label="Finish",
    description="Terminate the run.",
    parameters={"type": "object", "properties": {}},
    execute_fn=_finish,
)


def make_skill(**overrides) -> HarnessSpec:
    defaults = dict(
        name="demo",
        instructions="You are the demo skill.",
        actions=(ADVANCE, FINISH),
    )
    defaults.update(overrides)
    return HarnessSpec(**defaults)
