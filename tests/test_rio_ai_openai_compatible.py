"""Tests for the `/v1/responses` SSE parser in `rio.ai.openai_compatible`."""

from __future__ import annotations

import json
from itertools import count

from rio.ai._provider_events import ProviderErrorEvent, ProviderResponseEndEvent
from rio.ai.openai_compatible import _ResponsesStreamParser, _tool_to_openai, _tool_to_responses
from rio.ai.tools import AgentTool

_opaque_ids = count()


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


def test_tool_to_openai_keeps_the_supplied_schema():
    payload = _tool_to_openai(_read_like_tool(), {})

    function = payload["function"]
    assert function["strict"] is False
    assert function["parameters"] == _read_like_tool().parameters


def test_tool_to_openai_omits_strict_field_when_compat_opts_out():
    payload = _tool_to_openai(_read_like_tool(), {"supportsStrictMode": False})

    function = payload["function"]
    assert "strict" not in function
    # Falls back to the plain schema, unchanged.
    assert function["parameters"] == _read_like_tool().parameters


def test_tool_to_responses_keeps_the_supplied_schema():
    payload = _tool_to_responses(_read_like_tool(), {})

    assert payload["strict"] is False
    assert payload["parameters"] == _read_like_tool().parameters


def test_tool_to_responses_omits_strict_field_when_compat_opts_out():
    payload = _tool_to_responses(_read_like_tool(), {"supportsStrictMode": False})

    assert "strict" not in payload
    assert payload["parameters"] == _read_like_tool().parameters


def _opaque() -> str:
    """An item id of the shape GitHub Copilot returns: fresh on every event."""
    return f"encrypted-blob-{next(_opaque_ids)}"


def _function_call_events(output_index: int, name: str, arguments: str) -> list[dict]:
    call_id = f"call-{output_index}"
    return [
        {
            "type": "response.output_item.added",
            "output_index": output_index,
            "item": {
                "type": "function_call",
                "id": _opaque(),
                "call_id": call_id,
                "name": name,
                "arguments": "",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": output_index,
            "item_id": _opaque(),
            "delta": arguments,
        },
        {
            "type": "response.function_call_arguments.done",
            "output_index": output_index,
            "item_id": _opaque(),
            "name": name,
            "arguments": arguments,
        },
        {
            "type": "response.output_item.done",
            "output_index": output_index,
            "item": {
                "type": "function_call",
                "id": _opaque(),
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
            },
        },
    ]


def _parse(chunks: list[dict]):
    parser = _ResponsesStreamParser()
    for chunk in [*chunks, {"type": "response.completed", "response": {"status": "completed"}}]:
        parser.feed(json.dumps(chunk))
    end = next(e for e in parser.finalize() if isinstance(e, ProviderResponseEndEvent))
    return end.message.tool_calls


def test_rotating_item_ids_still_build_one_tool_call_per_output_index():
    """Copilot re-encrypts `item_id` per event; `output_index` is the identity.

    Keying builders by the id split one call into four -- two nameless
    fragments holding the arguments and two duplicates holding the name --
    which the SKILL.state loop then rejected as more than one `skill_step`.
    """
    chunks = _function_call_events(2, "skill_step", '{"action": {"name": "bash"}}')
    chunks += _function_call_events(3, "skill_step", '{"action": {"name": "read"}}')

    tool_calls = _parse(chunks)

    assert [call.name for call in tool_calls] == ["skill_step", "skill_step"]
    assert [call.id for call in tool_calls] == ["call-2", "call-3"]
    assert tool_calls[0].arguments == {"action": {"name": "bash"}}
    assert tool_calls[1].arguments == {"action": {"name": "read"}}


def test_arguments_events_alone_carry_name_and_are_ordered_by_output_index():
    """A provider that streams no `output_item` events still yields whole calls."""
    chunks = [
        chunk
        for output_index, arguments in ((1, '{"b": 2}'), (0, '{"a": 1}'))
        for chunk in _function_call_events(output_index, "skill_step", arguments)
        if chunk["type"].startswith("response.function_call_arguments")
    ]

    tool_calls = _parse(chunks)

    assert [call.name for call in tool_calls] == ["skill_step", "skill_step"]
    assert [call.arguments for call in tool_calls] == [{"a": 1}, {"b": 2}]


def _finalize(chunks: list[dict]):
    parser = _ResponsesStreamParser()
    for chunk in chunks:
        parser.feed(json.dumps(chunk))
    return parser.finalize()


def test_stream_without_terminal_event_ends_with_error():
    events = _finalize(_function_call_events(0, "read", '{"path": "a"}'))

    assert len(events) == 1
    assert isinstance(events[0], ProviderErrorEvent)
    assert "terminal response event" in events[0].message


def test_tool_call_without_done_event_ends_with_error():
    """Servers that omit output_index (llama.cpp) can leave calls mixed up; don't run them."""
    chunks = [
        chunk
        for chunk in _function_call_events(0, "read", '{"path": "a"}')
        if not chunk["type"].endswith(".done")
    ]
    chunks.append({"type": "response.completed", "response": {"status": "completed"}})

    events = _finalize(chunks)

    assert len(events) == 1
    assert isinstance(events[0], ProviderErrorEvent)
    assert "unfinished tool call: read (call-0)" in events[0].message


def test_shared_responses_input_preserves_tool_result_correlation_and_images():
    from rio.ai.messages import (
        AssistantMessage,
        ImageContent,
        TextContent,
        ToolCall,
        ToolResultMessage,
        UserMessage,
        portable_tool_call_id,
    )
    from rio.ai.openai_compatible import _messages_to_responses_input

    image = ImageContent(data="aGVsbG8=", mime_type="image/png")
    messages = [
        UserMessage(content=[TextContent(text="look"), image]),
        AssistantMessage(
            content=[
                TextContent(text="answer", text_signature="msg_1"),
                ToolCall(id="call_1|fc_1", name="read", arguments={}),
            ]
        ),
        ToolResultMessage(tool_call_id="call_1|fc_1", tool_name="read", content=[image]),
    ]
    for preserve_ids in (False, True):
        items = _messages_to_responses_input(
            messages, supports_images=True, preserve_ids=preserve_ids
        )
        assert items[0]["content"][1]["image_url"] == "data:image/png;base64,aGVsbG8="
        expected = "call_1" if preserve_ids else portable_tool_call_id("call_1|fc_1")
        assert items[2]["call_id"] == items[3]["call_id"] == expected
        assert items[3]["output"][0]["type"] == "input_image"
        if preserve_ids:
            assert items[1]["id"] == "msg_1"
            assert items[2]["id"] == "fc_1"
        else:
            assert "id" not in items[2]


def test_incomplete_responses_preserve_raw_reason_with_tools():
    events = _finalize(
        [
            *_function_call_events(0, "read", '{"path":'),
            {"type": "response.incomplete", "response": {"status": "incomplete"}},
        ]
    )
    # The shared collector maps the raw reason to length before checking tool use.
    assert events[-1].finish_reason == "incomplete"
    assert events[-1].message.tool_calls[0].arguments == {"_raw_arguments": '{"path":'}
