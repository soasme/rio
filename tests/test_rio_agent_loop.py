"""Tests for `run_skill_loop`, the SKILL.state algorithm (arXiv:2608.26263 Algorithm 1).

Uses `rio_ai.FakeProvider` to script deterministic model responses -- no
network, no API key -- so these assert the runtime's own guarantees:
state commits correctly, reasoning is never replayed, invalid proposals
roll back and retry, and per-step prompt size stays bounded regardless of
how many steps have already run.
"""

from __future__ import annotations

import pytest

from conftest import make_skill, step_response
from rio_agent import (
    ActionEndEvent,
    ReasoningDiscardedEvent,
    RetriesExhaustedError,
    RunEndEvent,
    StateUpdateEvent,
    StepEndEvent,
    ValidationErrorEvent,
    run_skill_loop,
)
from rio_ai import FakeProvider


@pytest.mark.asyncio
async def test_state_commits_across_steps_until_termination():
    skill = make_skill()
    provider = FakeProvider(
        [
            step_response(reasoning="r1", state_delta={"counter": 1}, action="advance", args={}),
            step_response(reasoning="r2", state_delta={"counter": 2}, action="finish", args={}),
        ]
    )

    events = [
        event
        async for event in run_skill_loop(
            provider=provider, model="m", skill=skill, observation="start"
        )
    ]

    state_updates = [e for e in events if isinstance(e, StateUpdateEvent)]
    assert [e.state["counter"] for e in state_updates] == [1, 2]

    run_end = events[-1]
    assert isinstance(run_end, RunEndEvent)
    assert run_end.steps == 2
    assert run_end.state["counter"] == 2


@pytest.mark.asyncio
async def test_termination_comes_from_action_result_not_max_steps():
    skill = make_skill()
    provider = FakeProvider(
        [step_response(reasoning="r1", state_delta={}, action="finish", args={})]
    )

    events = [
        event
        async for event in run_skill_loop(
            provider=provider, model="m", skill=skill, observation="start", max_steps=100
        )
    ]

    step_end = next(e for e in events if isinstance(e, StepEndEvent))
    assert step_end.terminated is True
    assert isinstance(events[-1], RunEndEvent)
    assert events[-1].steps == 1


@pytest.mark.asyncio
async def test_reasoning_is_surfaced_once_then_never_resent():
    skill = make_skill()
    provider = FakeProvider(
        [
            step_response(
                reasoning="SECRET_REASONING_1", state_delta={}, action="advance", args={}
            ),
            step_response(reasoning="SECRET_REASONING_2", state_delta={}, action="finish", args={}),
        ]
    )

    events = [
        event
        async for event in run_skill_loop(
            provider=provider, model="m", skill=skill, observation="start"
        )
    ]

    discarded = [e.reasoning for e in events if isinstance(e, ReasoningDiscardedEvent)]
    assert discarded == ["SECRET_REASONING_1", "SECRET_REASONING_2"]

    for _model, _system, messages, _tools in provider.calls:
        combined = " ".join(message.text for message in messages)
        assert "SECRET_REASONING_1" not in combined
        assert "SECRET_REASONING_2" not in combined


@pytest.mark.asyncio
async def test_prompt_footprint_is_bounded_across_steps():
    skill = make_skill()
    step_count = 25
    responses = [
        step_response(
            reasoning=f"r{i}", state_delta={"counter": i}, action="advance", args={"note": "x" * 50}
        )
        for i in range(step_count)
    ]
    responses.append(
        step_response(
            reasoning="last", state_delta={"counter": step_count}, action="finish", args={}
        )
    )
    provider = FakeProvider(responses)

    events = [
        event
        async for event in run_skill_loop(
            provider=provider,
            model="m",
            skill=skill,
            observation="start",
            max_steps=step_count + 1,
        )
    ]
    assert isinstance(events[-1], RunEndEvent)
    assert len(provider.calls) == step_count + 1

    message_counts = {len(messages) for _, _, messages, _ in provider.calls}
    assert message_counts == {1}, "each step must send exactly Σ_t + O_t, never a transcript"

    system_lengths = {len(system) for _, system, _, _ in provider.calls}
    assert system_lengths == {len(skill.instructions)}, "P (system) must never grow"

    body_lengths = [len(messages[0].text) for _, _, messages, _ in provider.calls]
    assert max(body_lengths) - min(body_lengths) < 200, (
        "per-step prompt body must stay roughly constant size, not grow with step count"
    )


@pytest.mark.asyncio
async def test_invalid_state_delta_rolls_back_and_retries():
    skill = make_skill()
    provider = FakeProvider(
        [
            step_response(
                reasoning="bad", state_delta={"unknown_field": 1}, action="advance", args={}
            ),
            step_response(reasoning="good", state_delta={"counter": 1}, action="finish", args={}),
        ]
    )

    events = [
        event
        async for event in run_skill_loop(
            provider=provider, model="m", skill=skill, observation="start", max_retries=1
        )
    ]

    validation_errors = [e for e in events if isinstance(e, ValidationErrorEvent)]
    assert len(validation_errors) == 1
    assert "unknown_field" in validation_errors[0].error
    assert len(provider.calls) == 2, "a rejected proposal must trigger a second model call"

    state_updates = [e for e in events if isinstance(e, StateUpdateEvent)]
    assert len(state_updates) == 1
    assert state_updates[0].delta == {"counter": 1}, "the rejected delta must never be committed"


@pytest.mark.asyncio
async def test_unknown_action_is_rejected():
    skill = make_skill()
    provider = FakeProvider(
        [
            step_response(reasoning="bad", state_delta={}, action="does_not_exist", args={}),
            step_response(reasoning="good", state_delta={}, action="finish", args={}),
        ]
    )

    events = [
        event
        async for event in run_skill_loop(
            provider=provider, model="m", skill=skill, observation="start", max_retries=1
        )
    ]

    validation_errors = [e for e in events if isinstance(e, ValidationErrorEvent)]
    assert any("does_not_exist" in e.error for e in validation_errors)
    assert any(isinstance(e, ActionEndEvent) and e.name == "finish" for e in events)


@pytest.mark.asyncio
async def test_retries_exhausted_raises():
    skill = make_skill()
    provider = FakeProvider(
        [
            step_response(reasoning="bad1", state_delta={"unknown": 1}, action="advance", args={}),
            step_response(reasoning="bad2", state_delta={"unknown": 1}, action="advance", args={}),
        ]
    )

    with pytest.raises(RetriesExhaustedError):
        async for _ in run_skill_loop(
            provider=provider, model="m", skill=skill, observation="start", max_retries=1
        ):
            pass


@pytest.mark.asyncio
async def test_missing_skill_step_tool_call_is_rejected():
    from rio_ai import AssistantDoneEvent, AssistantMessage, TextContent

    skill = make_skill()
    plain_message = AssistantMessage(content=[TextContent(text="no tool call")], stop_reason="stop")
    provider = FakeProvider([[AssistantDoneEvent(reason="stop", message=plain_message)]])

    with pytest.raises(RetriesExhaustedError):
        async for _ in run_skill_loop(
            provider=provider, model="m", skill=skill, observation="start", max_retries=0
        ):
            pass
