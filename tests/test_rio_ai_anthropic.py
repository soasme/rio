"""Tests for strict/constrained tool-schema wiring in `rio.ai.anthropic`."""

from __future__ import annotations

from rio.ai.anthropic import _anthropic_tool
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


def test_anthropic_tool_uses_strict_schema_when_compat_allows_it():
    payload = _anthropic_tool(_read_like_tool(), compat={})

    assert payload["strict"] is True
    assert payload["input_schema"]["additionalProperties"] is False
    assert payload["input_schema"]["required"] == ["path", "offset"]
    assert payload["input_schema"]["type"] == "object"
    assert payload["input_schema"]["properties"]["offset"] == {
        "anyOf": [{"type": "integer"}, {"type": "null"}]
    }


def test_anthropic_tool_falls_back_to_plain_schema_when_compat_opts_out():
    payload = _anthropic_tool(_read_like_tool(), compat={"supportsStrictMode": False})

    assert "strict" not in payload
    assert payload["input_schema"] == _read_like_tool().input_schema


def test_anthropic_tool_keeps_cache_control():
    cache_control = {"type": "ephemeral"}
    payload = _anthropic_tool(_read_like_tool(), cache_control=cache_control, compat={})

    assert payload["cache_control"] == cache_control
