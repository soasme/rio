"""Sanity tests for the ported rio.ai vocabulary and FakeProvider.

These don't hit any network; they confirm the tau_ai -> rio.ai port holds
together end to end (message/tool models construct and round-trip through
a ModelProvider implementation) before rio.agent builds on top of it.
"""

from __future__ import annotations

import pytest

from rio.ai import (
    AgentToolResult,
    AssistantDoneEvent,
    AssistantMessage,
    FakeProvider,
    TextContent,
    ToolCall,
    UserMessage,
)


@pytest.mark.asyncio
async def test_fake_provider_replays_scripted_stream():
    message = AssistantMessage(content=[TextContent(text="hi")], stop_reason="stop")
    provider = FakeProvider([[AssistantDoneEvent(reason="stop", message=message)]])

    events = [
        event
        async for event in provider.stream_response(
            model="fake-model",
            system="be nice",
            messages=[UserMessage(content="hello")],
            tools=[],
        )
    ]

    assert len(events) == 1
    assert events[0].message.text == "hi"
    model, system, messages, tools = provider.calls[0]
    assert model == "fake-model"
    assert system == "be nice"
    assert len(messages) == 1
    assert messages[0].text == "hello"
    assert tools == []


@pytest.mark.asyncio
async def test_fake_provider_stops_on_cancellation():
    class AlreadyCancelled:
        def is_cancelled(self) -> bool:
            return True

    provider = FakeProvider([[AssistantDoneEvent(reason="stop", message=AssistantMessage())]])

    events = [
        event
        async for event in provider.stream_response(
            model="m", system="s", messages=[], tools=[], signal=AlreadyCancelled()
        )
    ]

    assert events == []


def test_assistant_message_exposes_text_and_tool_calls():
    call = ToolCall(id="1", name="do_it", arguments={"x": 1})
    message = AssistantMessage(content=[TextContent(text="thinking"), call], stop_reason="toolUse")

    assert message.text == "thinking"
    assert message.tool_calls == (call,)


def test_agent_tool_result_text_property_concatenates_text_blocks():
    result = AgentToolResult(content=[TextContent(text="a"), TextContent(text="b")])

    assert result.text == "ab"
