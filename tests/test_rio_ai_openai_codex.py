"""Tests for strict/constrained tool-schema wiring in `rio.ai.openai_codex`."""

from __future__ import annotations

from rio.ai.openai_codex import _tool_to_codex
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


def test_tool_to_codex_uses_strict_schema_by_default():
    payload = _tool_to_codex(_read_like_tool())

    assert payload["strict"] is True
    assert payload["parameters"]["additionalProperties"] is False
    assert payload["parameters"]["required"] == ["path", "offset"]


def test_tool_to_codex_omits_strict_when_compat_opts_out():
    payload = _tool_to_codex(_read_like_tool(), {"supportsStrictMode": False})

    assert payload["strict"] is False
    assert payload["parameters"] == _read_like_tool().input_schema
