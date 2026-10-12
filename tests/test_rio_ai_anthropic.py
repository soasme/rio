"""Tests for tool-schema wiring in `rio.ai.anthropic`."""

from __future__ import annotations

from rio.ai.anthropic import _anthropic_tool
from rio.ai.tools import AgentTool


def _read_like_tool() -> AgentTool:
    return AgentTool(
        name="read",
        description="Read a file",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}},
            "required": ["path"],
        },
    )


def test_anthropic_tool_keeps_the_supplied_schema():
    payload = _anthropic_tool(_read_like_tool())
    assert "strict" not in payload
    assert payload["input_schema"] == _read_like_tool().parameters


def test_anthropic_tool_keeps_cache_control():
    cache_control = {"type": "ephemeral"}
    payload = _anthropic_tool(_read_like_tool(), cache_control=cache_control)

    assert payload["cache_control"] == cache_control
