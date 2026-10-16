"""Sanity tests for the rio.ai vocabulary and FakeProvider, without a network."""

from __future__ import annotations

import pytest

from rio.ai import (
    AssistantMessage,
    FakeProvider,
    TextContent,
    ToolCall,
    UserMessage,
)


@pytest.mark.asyncio
async def test_fake_provider_returns_scripted_messages_then_an_error():
    message = AssistantMessage(content=[TextContent(text="hi")], stop_reason="stop")
    provider = FakeProvider([message])
    request = {
        "model": "fake-model",
        "system": "be nice",
        "messages": [UserMessage(content="hello")],
        "tools": [],
    }

    assert (await provider.complete(**request)).text == "hi"
    exhausted = await provider.complete(**request)

    assert exhausted.stop_reason == "error"
    model, system, messages, tools = provider.calls[0]
    assert model == "fake-model"
    assert system == "be nice"
    assert [m.content for m in messages] == ["hello"]
    assert tools == []


def test_assistant_message_exposes_text_and_tool_calls():
    call = ToolCall(id="1", name="do_it", arguments={"x": 1})
    message = AssistantMessage(content=[TextContent(text="thinking"), call], stop_reason="toolUse")

    assert message.text == "thinking"
    assert message.tool_calls == (call,)


def test_saved_responses_from_older_journals_still_validate():
    saved = {
        "content": [
            {"type": "thinking", "thinking": "t", "thinkingSignature": "sig", "redacted": False},
            {"type": "text", "text": "x", "textSignature": None},
            {
                "type": "toolCall",
                "id": "c",
                "name": "step",
                "arguments": {},
                "thoughtSignature": None,
            },
        ]
    }
    message = AssistantMessage.model_validate(saved)

    assert [block.type for block in message.content] == ["thinking", "text", "toolCall"]
    assert "Signature" not in str(message.model_dump(mode="json"))
