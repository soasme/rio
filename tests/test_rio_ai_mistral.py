"""Tests for tool-schema wiring in `rio.ai.mistral`."""

from __future__ import annotations

from rio.ai.mistral import _tool_to_mistral
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


def test_tool_to_mistral_keeps_the_supplied_schema() -> None:
    payload = _tool_to_mistral(_read_like_tool())

    function = payload["function"]
    assert isinstance(function, dict)
    assert function["strict"] is False
    assert function["parameters"] == _read_like_tool().parameters


def test_tool_to_mistral_keeps_empty_object_schema() -> None:
    tool = AgentTool(
        name="custom",
        description="Custom tool",
        parameters={"type": "object", "properties": {"value": {"type": "string"}}},
    )

    payload = _tool_to_mistral(tool)

    function = payload["function"]
    assert isinstance(function, dict)
    assert function["strict"] is False
    assert function["parameters"] == tool.parameters


def test_mistral_medium_reasoning_models_use_reasoning_effort() -> None:
    from rio.ai.mistral import _build_mistral_payload

    for model in ("mistral-medium-2604", "mistral-medium-latest", "mistral-medium-3.5"):
        payload = _build_mistral_payload(
            model=model,
            system="",
            messages=[],
            tools=[],
            reasoning_effort="high",
            max_tokens=None,
        )

        assert payload["reasoning_effort"] == "high", model
        assert "prompt_mode" not in payload, model


def test_magistral_uses_prompt_mode() -> None:
    from rio.ai.mistral import _build_mistral_payload

    payload = _build_mistral_payload(
        model="magistral-medium-latest",
        system="",
        messages=[],
        tools=[],
        reasoning_effort="high",
        max_tokens=None,
    )

    assert payload["prompt_mode"] == "reasoning"
    assert "reasoning_effort" not in payload
