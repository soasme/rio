"""Tests for `collect_response`, the provider-neutral bridge."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from rio.ai._provider_events import ProviderEvent, ProviderResponseEndEvent, ProviderToolCallEvent
from rio.ai.messages import AssistantMessage, ToolCall, malformed_tool_arguments
from rio.ai.provider import AssistantDoneEvent, collect_response


async def _canonicalize(events: list[ProviderEvent]) -> AssistantMessage:
    async def source() -> AsyncIterator[ProviderEvent]:
        for event in events:
            yield event

    done = [
        event
        async for event in collect_response(
            source(), api="anthropic-messages", provider="anthropic", model="m"
        )
        if isinstance(event, AssistantDoneEvent)
    ]
    return done[-1].message


@pytest.mark.asyncio
async def test_truncation_wins_over_tool_use_in_the_stop_reason():
    """A run cut off mid tool call is `length`, not `toolUse`.

    Truncation almost always lands inside a tool call, so a `has_tools` check
    that ran first labelled every one of them `toolUse` -- erasing the only
    signal that the call's arguments are incomplete.
    """
    partial = ToolCall(
        id="call-0", name="skill_step", arguments=malformed_tool_arguments('{"action": {"na')
    )
    message = await _canonicalize(
        [
            ProviderToolCallEvent(tool_call=partial),
            ProviderResponseEndEvent(
                message=AssistantMessage(content=[partial]), finish_reason="max_tokens"
            ),
        ]
    )

    assert message.stop_reason == "length"
    assert message.tool_calls


@pytest.mark.asyncio
async def test_a_complete_tool_call_is_still_reported_as_tool_use():
    call = ToolCall(id="call-0", name="skill_step", arguments={"action": {"name": "advance"}})
    message = await _canonicalize(
        [
            ProviderToolCallEvent(tool_call=call),
            ProviderResponseEndEvent(
                message=AssistantMessage(content=[call]), finish_reason="tool_use"
            ),
        ]
    )

    assert message.stop_reason == "toolUse"


@pytest.mark.asyncio
async def test_collector_preserves_order_metadata_and_usage_without_snapshots(monkeypatch):
    from rio.ai._provider_events import ProviderTextDeltaEvent, ProviderThinkingDeltaEvent
    from rio.ai.messages import ThinkingContent, Usage

    def reject_copy(*args, **kwargs):
        raise AssertionError("partial messages must never be copied")

    monkeypatch.setattr(AssistantMessage, "model_copy", reject_copy)
    message = await _canonicalize(
        [
            *(ProviderTextDeltaEvent(delta="x") for _ in range(1000)),
            ProviderThinkingDeltaEvent(delta="reason"),
            ProviderTextDeltaEvent(delta="after"),
            ProviderResponseEndEvent(
                message=AssistantMessage(
                    content=[ThinkingContent(thinking="reason", thinking_signature="opaque")],
                    usage=Usage(input=4, output=8),
                )
            ),
        ]
    )
    assert [block.type for block in message.content] == ["text", "thinking", "text"]
    assert message.text == "x" * 1000 + "after"
    assert message.content[1].thinking_signature == "opaque"
    assert message.usage.input == 4
    assert message.usage.output == 8


@pytest.mark.asyncio
async def test_collector_emits_one_error_when_stream_has_no_terminal_event():
    from rio.ai._provider_events import ProviderTextDeltaEvent

    async def source():
        yield ProviderTextDeltaEvent(delta="partial")

    events = [event async for event in collect_response(source(), api="a", provider="p", model="m")]
    assert len(events) == 1
    assert events[0].type == "error"
    assert events[0].error.text == "partial"
    assert "terminal event" in events[0].error.error_message
