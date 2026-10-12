"""Tests for Responses parsing and payloads in `rio.ai.openai_codex`."""

from __future__ import annotations

from rio.ai.openai_codex import _tool_to_codex
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


def test_tool_to_codex_keeps_the_supplied_schema():
    payload = _tool_to_codex(_read_like_tool())

    assert payload["strict"] is False
    assert payload["parameters"] == _read_like_tool().parameters


def _sse_response(events: list[dict]):
    import json

    import httpx

    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    return httpx.Response(200, content=body.encode())


async def _collect(events: list[dict]):
    from rio.ai.openai_codex import _codex_provider_events

    return [event async for event in _codex_provider_events(_sse_response(events))]


_ADDED_CALL = {
    "type": "response.output_item.added",
    "output_index": 0,
    "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "read"},
}
_COMPLETED = {"type": "response.completed", "response": {"status": "completed"}}


async def test_codex_stream_without_terminal_event_ends_with_error():
    from rio.ai._provider_events import ProviderErrorEvent

    events = await _collect([{"type": "response.output_text.delta", "delta": "hi"}])

    assert isinstance(events[-1], ProviderErrorEvent)
    assert "terminal response event" in events[-1].message


async def test_codex_unfinished_tool_call_ends_with_error():
    from rio.ai._provider_events import ProviderErrorEvent

    events = await _collect([_ADDED_CALL, _COMPLETED])

    assert isinstance(events[-1], ProviderErrorEvent)
    assert "unfinished tool call: read (call_1)" in events[-1].message


async def test_codex_finished_tool_call_is_returned():
    from rio.ai._provider_events import ProviderResponseEndEvent

    done = {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": {**_ADDED_CALL["item"], "arguments": '{"path": "a"}'},
    }
    events = await _collect([_ADDED_CALL, done, _COMPLETED])

    assert isinstance(events[-1], ProviderResponseEndEvent)
    assert events[-1].message.tool_calls[0].arguments == {"path": "a"}


def test_codex_model_at_capacity_stream_error_is_retryable():
    from rio.ai._provider_events import ProviderErrorEvent
    from rio.ai.openai_codex import _retryable_stream_error_event

    event = ProviderErrorEvent(
        message="x",
        data={"event": {"type": "error", "error": {"message": "Selected model is at capacity"}}},
    )

    assert _retryable_stream_error_event(event)


def test_codex_subscription_usage_limit_is_not_retryable():
    from rio.ai.openai_codex import _is_retryable_status

    assert not _is_retryable_status(429, '{"code": "subscription_sharing_usage_limit_exceeded"}')


async def test_codex_aliases_usage_and_replay_ids_survive_shared_parser():
    import json

    from rio.ai.messages import ThinkingContent
    from rio.ai.openai_codex import _build_codex_payload

    reasoning = {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}
    events = await _collect(
        [
            {"type": "response.reasoning.delta", "delta": "think"},
            {"type": "response.output_item.done", "item": reasoning},
            _ADDED_CALL,
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_1",
                "delta": '{"path":',
            },
            {
                "type": "response.function_call_arguments.delta",
                "call_id": "call_1",
                "delta": '"a"}',
            },
            {"type": "response.output_item.completed", "item": _ADDED_CALL["item"]},
            {
                "type": "response.done",
                "response": {
                    "status": "completed",
                    "usage": {
                        "input_tokens": 20,
                        "output_tokens": 9,
                        "total_tokens": 29,
                        "input_tokens_details": {"cached_tokens": 5, "cache_write_tokens": 2},
                        "output_tokens_details": {"reasoning_tokens": 3},
                    },
                },
            },
        ]
    )
    message = events[-1].message
    assert message.tool_calls[0].id == "call_1|fc_1"
    assert message.tool_calls[0].arguments == {"path": "a"}
    assert (
        message.usage.input,
        message.usage.output,
        message.usage.cache_read,
        message.usage.cache_write,
        message.usage.reasoning,
        message.usage.total_tokens,
    ) == (13, 9, 5, 2, 3, 29)
    assert isinstance(message.content[0], ThinkingContent)
    assert json.loads(message.content[0].thinking_signature) == reasoning
    payload = _build_codex_payload(model="m", system="s", messages=[message], tools=[])
    assert payload["input"][0] == reasoning
    assert payload["input"][1]["id"] == "fc_1"
    assert payload["input"][1]["call_id"] == "call_1"


async def test_codex_arguments_done_is_not_a_completed_tool():
    events = await _collect(
        [
            _ADDED_CALL,
            {"type": "response.function_call_arguments.done", "output_index": 0, "arguments": "{}"},
            _COMPLETED,
        ]
    )
    assert events[-1].type == "error"
    assert "unfinished tool call" in events[-1].message


async def test_codex_completed_message_fallback_and_multiline_sse():
    import httpx

    from rio.ai.openai_codex import _codex_provider_events

    response = httpx.Response(
        200,
        content=(
            'data: {"type":"response.output_item.completed",\n'
            'data: "item":{"type":"message","content":[{"type":"output_text","text":"hi"}]}}\n\n'
            'data: {"type":"response.done","response":{"status":"completed"}}\n\n'
        ),
    )
    events = [event async for event in _codex_provider_events(response)]
    assert events[-1].message.text == "hi"


async def test_codex_done_event_without_item_does_not_complete_a_tool():
    events = await _collect(
        [
            _ADDED_CALL,
            {"type": "response.output_item.done", "output_index": 0},
            _COMPLETED,
        ]
    )
    assert events[-1].type == "error"
    assert "unfinished tool call" in events[-1].message
