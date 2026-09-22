"""Tests for the `/v1/responses` SSE parser in `rio.ai.openai_compatible`."""

from __future__ import annotations

import json
from itertools import count

from rio.ai._provider_events import ProviderResponseEndEvent
from rio.ai.openai_compatible import _ResponsesStreamParser, _tool_to_openai, _tool_to_responses
from rio.ai.tools import AgentTool

_opaque_ids = count()


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


def test_tool_to_openai_uses_strict_schema_when_compat_allows_it():
    payload = _tool_to_openai(_read_like_tool(), {})

    function = payload["function"]
    assert function["strict"] is True
    assert function["parameters"]["additionalProperties"] is False
    assert function["parameters"]["required"] == ["path", "offset"]
    assert function["parameters"]["properties"]["offset"] == {
        "anyOf": [{"type": "integer"}, {"type": "null"}]
    }


def test_tool_to_openai_omits_strict_field_when_compat_opts_out():
    payload = _tool_to_openai(_read_like_tool(), {"supportsStrictMode": False})

    function = payload["function"]
    assert "strict" not in function
    # Falls back to the plain schema, unchanged.
    assert function["parameters"] == _read_like_tool().input_schema


def test_tool_to_responses_uses_strict_schema_when_compat_allows_it():
    payload = _tool_to_responses(_read_like_tool(), {})

    assert payload["strict"] is True
    assert payload["parameters"]["additionalProperties"] is False


def test_tool_to_responses_omits_strict_field_when_compat_opts_out():
    payload = _tool_to_responses(_read_like_tool(), {"supportsStrictMode": False})

    assert "strict" not in payload
    assert payload["parameters"] == _read_like_tool().input_schema


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
    for chunk in chunks:
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
