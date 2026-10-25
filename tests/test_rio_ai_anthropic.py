"""Tests for tool-schema and cache wiring in `rio.ai.anthropic`."""

from __future__ import annotations

from typing import cast

from rio.ai.anthropic import _anthropic_tool, _build_messages_payload
from rio.ai.messages import TextContent, UserMessage
from rio.ai.tools import AgentTool
from rio.ai.types import JSONValue

EPHEMERAL = {"type": "ephemeral"}


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


def test_anthropic_tool_keeps_the_supplied_schema() -> None:
    payload = _anthropic_tool(_read_like_tool())
    assert "strict" not in payload
    assert payload["input_schema"] == _read_like_tool().parameters


def test_anthropic_tool_keeps_cache_control() -> None:
    cache_control: dict[str, JSONValue] = {"type": "ephemeral"}
    payload = _anthropic_tool(_read_like_tool(), cache_control=cache_control)

    assert payload["cache_control"] == cache_control


def _payload(messages: list[UserMessage]) -> dict[str, JSONValue]:
    return _build_messages_payload(
        model="m", system="s", oauth_system_prompt=None, messages=messages, tools=[]
    )


def test_anthropic_marks_the_cache_flagged_message_and_keeps_its_blocks() -> None:
    stable = UserMessage(content=[TextContent(text="a"), TextContent(text="b")], cache=True)
    payload = _payload([stable, UserMessage(content="tail"), UserMessage(content="retry")])
    blocks = [message["content"] for message in cast(list[dict], payload["messages"])]
    assert blocks == [
        [{"type": "text", "text": "a"}, {"type": "text", "text": "b", "cache_control": EPHEMERAL}],
        [{"type": "text", "text": "tail"}],
        [{"type": "text", "text": "retry"}],
    ]


def test_anthropic_marks_the_last_message_when_none_is_flagged() -> None:
    payload = _payload([UserMessage(content="a"), UserMessage(content="b")])
    blocks = [message["content"] for message in cast(list[dict], payload["messages"])]
    assert blocks == [
        [{"type": "text", "text": "a"}],
        [{"type": "text", "text": "b", "cache_control": EPHEMERAL}],
    ]
