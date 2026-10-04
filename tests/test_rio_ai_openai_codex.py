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


def _sse_response(events: list[dict]):
    import json

    import httpx

    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    return httpx.Response(200, content=body.encode())


async def _collect(events: list[dict]):
    from rio.ai.openai_codex import _codex_provider_events

    return [event async for event in _codex_provider_events(_sse_response(events), signal=None)]


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
