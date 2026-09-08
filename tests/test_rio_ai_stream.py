"""Tests for `canonicalize_provider_stream`, the provider-neutral bridge."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from rio_ai._provider_events import ProviderEvent, ProviderResponseEndEvent, ProviderToolCallEvent
from rio_ai.messages import AssistantMessage, ToolCall, malformed_tool_arguments
from rio_ai.provider_events import AssistantDoneEvent
from rio_ai.stream import canonicalize_provider_stream


async def _canonicalize(events: list[ProviderEvent]) -> AssistantMessage:
    async def source() -> AsyncIterator[ProviderEvent]:
        for event in events:
            yield event

    done = [
        event
        async for event in canonicalize_provider_stream(
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
