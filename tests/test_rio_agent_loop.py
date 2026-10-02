"""Tests for `run_context_loop`, the CLM loop (arXiv:2609.37725).

Uses `rio.ai.FakeProvider` to script deterministic model responses -- no
network, no API key -- so these assert the runtime's own guarantees: the
context is mirrored to a file, the model's edits to that file become its next
context, unusable replies are retried, and actions never crash the loop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FINISH, make_skill, step_response
from rio.agent import (
    ActionEndEvent,
    ContextEditEvent,
    ProviderResponseError,
    ReasoningEvent,
    RetriesExhaustedError,
    RunEndEvent,
    StepEndEvent,
    ValidationErrorEvent,
    parse_context,
    render_context,
    run_context_loop,
    turn,
)
from rio.ai import (
    AgentTool,
    AgentToolResult,
    AssistantDoneEvent,
    AssistantErrorEvent,
    AssistantMessage,
    FakeProvider,
    TextContent,
    ToolCall,
    malformed_tool_arguments,
)


async def _run(provider, *, skill=None, **kwargs) -> list:
    return [
        event
        async for event in run_context_loop(
            provider=provider, model="m", skill=skill or make_skill(), **kwargs
        )
    ]


def _rewrite_tool(path: Path, text: str) -> AgentTool:
    """A tool that overwrites the context file, as a model would with `write` or `bash`."""

    async def execute(tool_call_id, arguments, signal=None, on_update=None):
        path.write_text(text)
        return AgentToolResult(content=[TextContent(text="")])

    return AgentTool(
        name="rewrite",
        label="Rewrite",
        description="Rewrite the context file.",
        parameters={"type": "object", "properties": {}},
        execute_fn=execute,
    )


@pytest.mark.asyncio
async def test_each_step_appends_the_reply_and_observation_to_the_context():
    provider = FakeProvider(
        [
            step_response(reasoning="look first", action="advance", args={"note": "a"}),
            step_response(reasoning="done", action="finish"),
        ]
    )

    events = await _run(provider, observation="do the task")

    run_end = events[-1]
    assert isinstance(run_end, RunEndEvent)
    assert run_end.steps == 2
    roles = [item["role"] for item in run_end.context]
    assert roles == ["user", "assistant", "tool", "assistant", "tool"]
    assert run_end.context[1]["text"] == 'look first\nadvance {"note": "a"}'
    assert run_end.context[2]["text"].startswith("observed:a\n[context: ~")


@pytest.mark.asyncio
async def test_the_model_sees_its_context_as_messages_and_its_reasoning_stays():
    provider = FakeProvider(
        [
            step_response(reasoning="REMEMBER", action="advance"),
            step_response(action="finish"),
        ]
    )

    events = await _run(provider, observation="do the task")

    assert any(isinstance(e, ReasoningEvent) and e.reasoning == "REMEMBER" for e in events)
    second_request = provider.calls[1][2]
    assert [message.role for message in second_request] == ["user", "assistant", "user"]
    assert "REMEMBER" in second_request[1].text


@pytest.mark.asyncio
async def test_the_context_is_mirrored_to_a_file_named_in_the_system_prompt(tmp_path):
    path = tmp_path / "CONTEXT.md"
    provider = FakeProvider([step_response(action="finish")])

    await _run(provider, observation="do the task", context_file=path)

    system = provider.calls[0][1]
    assert str(path) in system
    assert "[[CTX_TURN" in system
    assert parse_context(path.read_text()) == [turn("user", "do the task")]


@pytest.mark.asyncio
async def test_an_edit_to_the_context_file_becomes_the_next_context(tmp_path):
    path = tmp_path / "CONTEXT.md"
    compacted = render_context([turn("notes", "task: ship it; found the bug in a.py")])
    skill = make_skill(actions=(_rewrite_tool(path, compacted), FINISH))
    provider = FakeProvider([step_response(action="rewrite"), step_response(action="finish")])

    events = await _run(provider, skill=skill, observation="x" * 2000, context_file=path)

    edit = next(e for e in events if isinstance(e, ContextEditEvent))
    assert edit.accepted
    assert edit.after_tokens < edit.before_tokens
    second_request = provider.calls[1][2]
    assert "x" * 2000 not in second_request[0].text
    assert "found the bug in a.py" in second_request[0].text
    final = events[-1].context
    assert final[0] == turn("notes", "task: ship it; found the bug in a.py")
    assert "[context edit applied" in final[2]["text"]


@pytest.mark.asyncio
async def test_an_edit_over_the_limit_is_rejected(tmp_path):
    path = tmp_path / "CONTEXT.md"
    skill = make_skill(actions=(_rewrite_tool(path, "y" * 4000), FINISH))
    provider = FakeProvider([step_response(action="rewrite"), step_response(action="finish")])

    events = await _run(
        provider,
        skill=skill,
        observation="task",
        context_file=path,
        context_window_tokens=1000,
    )

    edit = next(e for e in events if isinstance(e, ContextEditEvent))
    assert not edit.accepted
    final = events[-1].context
    assert final[0] == turn("user", "task")
    assert "[context edit rejected" in final[2]["text"]


@pytest.mark.asyncio
async def test_a_context_over_the_limit_withholds_the_oldest_observations():
    async def _big(tool_call_id, arguments, signal=None, on_update=None):
        return AgentToolResult(content=[TextContent(text="z" * 2400)])

    big = AgentTool(
        name="advance",
        label="Big",
        description="Produce a lot of output.",
        parameters={"type": "object", "properties": {}},
        execute_fn=_big,
    )
    skill = make_skill(actions=(big, FINISH))
    provider = FakeProvider(
        [
            step_response(action="advance"),
            step_response(action="advance"),
            step_response(action="finish"),
        ]
    )

    events = await _run(provider, skill=skill, observation="task", context_window_tokens=1200)

    third_request = provider.calls[2][2]
    text = "\n".join(message.text for message in third_request)
    assert "withheld by the runtime" in text
    assert text.count("z" * 2400) == 1
    assert isinstance(events[-1], RunEndEvent)


@pytest.mark.asyncio
async def test_each_observation_reports_the_context_size_and_asks_to_compact_near_the_limit():
    provider = FakeProvider([step_response(action="advance"), step_response(action="finish")])

    events = await _run(provider, observation="t" * 2400, context_window_tokens=1000)

    observation = next(e for e in events if isinstance(e, StepEndEvent)).context[-1]["text"]
    assert "/800 tokens -- compact " in observation


@pytest.mark.asyncio
async def test_termination_comes_from_the_action_result():
    provider = FakeProvider([step_response(action="finish")])

    events = await _run(provider, observation="start", max_steps=100)

    step_end = next(e for e in events if isinstance(e, StepEndEvent))
    assert step_end.terminated is True
    assert events[-1].steps == 1


@pytest.mark.asyncio
async def test_a_reply_without_a_tool_call_is_retried_with_a_transient_correction():
    no_call = AssistantMessage(content=[TextContent(text="I am done")], stop_reason="stop")
    provider = FakeProvider(
        [[AssistantDoneEvent(reason="stop", message=no_call)], step_response(action="finish")]
    )

    events = await _run(provider, observation="start")

    error = next(e for e in events if isinstance(e, ValidationErrorEvent)).error
    assert "call exactly one tool" in error
    assert "Rejected reply" in provider.calls[1][2][-1].text
    assert "Rejected reply" not in render_context(events[-1].context)


@pytest.mark.asyncio
async def test_an_unknown_tool_is_rejected():
    provider = FakeProvider(
        [step_response(action="does_not_exist"), step_response(action="finish")]
    )

    events = await _run(provider, observation="start")

    error = next(e for e in events if isinstance(e, ValidationErrorEvent)).error
    assert "unknown tool 'does_not_exist'" in error


@pytest.mark.asyncio
async def test_retries_exhausted_raises():
    provider = FakeProvider([step_response(action="nope"), step_response(action="nope")])

    with pytest.raises(RetriesExhaustedError):
        await _run(provider, observation="start", max_retries=1)


@pytest.mark.asyncio
async def test_a_provider_error_is_raised_instead_of_retried():
    error = AssistantMessage(content=[], stop_reason="error", error_message="429 rate limited")
    provider = FakeProvider([[AssistantErrorEvent(reason="error", error=error)]])

    with pytest.raises(ProviderResponseError, match="429 rate limited"):
        await _run(provider, observation="start")


@pytest.mark.asyncio
async def test_a_failing_action_becomes_an_observation_instead_of_crashing():
    async def _explode(tool_call_id, arguments, signal=None, on_update=None):
        raise RuntimeError("boom")

    explode = AgentTool(
        name="advance",
        label="Explode",
        description="Fail.",
        parameters={"type": "object", "properties": {}},
        execute_fn=_explode,
    )
    skill = make_skill(actions=(explode, FINISH))
    provider = FakeProvider([step_response(action="advance"), step_response(action="finish")])

    events = await _run(provider, skill=skill, observation="start")

    failed = next(e for e in events if isinstance(e, ActionEndEvent) and e.name == "advance")
    assert failed.is_error
    assert "advance failed: boom" in provider.calls[1][2][-1].text


@pytest.mark.asyncio
async def test_only_the_first_of_several_tool_calls_runs():
    message = AssistantMessage(
        content=[
            ToolCall(id="a", name="advance", arguments={"note": "1"}),
            ToolCall(id="b", name="advance", arguments={"note": "2"}),
        ],
        stop_reason="toolUse",
    )
    provider = FakeProvider(
        [[AssistantDoneEvent(reason="toolUse", message=message)], step_response(action="finish")]
    )

    events = await _run(provider, observation="start")

    ran = [e for e in events if isinstance(e, ActionEndEvent)]
    assert [e.result.text for e in ran] == ["observed:1", "terminal"]
    assert "only the first tool call ran" in provider.calls[1][2][-1].text


@pytest.mark.asyncio
async def test_a_truncated_call_is_reported_as_truncation():
    partial = '{"note": "aaa'
    message = AssistantMessage(
        content=[ToolCall(id="c", name="advance", arguments=malformed_tool_arguments(partial))],
        stop_reason="length",
    )
    provider = FakeProvider(
        [[AssistantDoneEvent(reason="length", message=message)], step_response(action="finish")]
    )

    events = await _run(provider, observation="start", max_retries=1)

    error = next(e for e in events if isinstance(e, ValidationErrorEvent)).error
    assert "output token limit" in error
    assert str(len(partial)) in error
