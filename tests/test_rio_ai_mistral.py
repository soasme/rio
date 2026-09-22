"""Tests for strict/constrained tool-schema wiring in `rio.ai.mistral`."""

from __future__ import annotations

from rio.ai.mistral import _tool_to_mistral
from rio.ai.tools import AgentTool


async def _noop_execute(tool_call_id, arguments, signal=None, on_update=None):
    raise NotImplementedError


def _read_like_tool() -> AgentTool:
    return AgentTool(
        name="read",
        label="read",
        description="Read a file",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}},
            "required": ["path"],
        },
        execute_fn=_noop_execute,
        constrained_sampling={"type": "json_schema", "strict": "prefer"},
    )


def test_tool_to_mistral_always_uses_strict_schema():
    payload = _tool_to_mistral(_read_like_tool())

    function = payload["function"]
    assert function["strict"] is True
    assert function["parameters"]["additionalProperties"] is False
    assert function["parameters"]["required"] == ["path", "offset"]


def test_tool_to_mistral_falls_back_when_tool_did_not_opt_in():
    tool = AgentTool(
        name="custom",
        label="custom",
        description="Custom tool",
        parameters={"type": "object", "properties": {"value": {"type": "string"}}},
        execute_fn=_noop_execute,
    )

    payload = _tool_to_mistral(tool)

    function = payload["function"]
    assert function["strict"] is False
    assert function["parameters"] == tool.input_schema
