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
    HarnessObservation,
    ProviderResponseError,
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
            provider=provider,
            model="m",
            skill=skill,
            observation=HarnessObservation(user_message="start"),
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
            provider=provider,
            model="m",
            skill=skill,
            observation=HarnessObservation(user_message="start"),
            max_steps=100,
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
            provider=provider,
            model="m",
            skill=skill,
            observation=HarnessObservation(user_message="start"),
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
            observation=HarnessObservation(user_message="start"),
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
            provider=provider,
            model="m",
            skill=skill,
            observation=HarnessObservation(user_message="start"),
            max_retries=1,
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
            provider=provider,
            model="m",
            skill=skill,
            observation=HarnessObservation(user_message="start"),
            max_retries=1,
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
            provider=provider,
            model="m",
            skill=skill,
            observation=HarnessObservation(user_message="start"),
            max_retries=1,
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
            provider=provider,
            model="m",
            skill=skill,
            observation=HarnessObservation(user_message="start"),
            max_retries=0,
        ):
            pass


@pytest.mark.asyncio
async def test_a_wrongly_named_tool_call_names_what_was_called():
    from rio_ai import AssistantDoneEvent, AssistantMessage, ToolCall

    skill = make_skill()
    message = AssistantMessage(
        content=[ToolCall(id="call-0", name="advance", arguments={})],
        stop_reason="toolUse",
    )
    provider = FakeProvider([[AssistantDoneEvent(reason="toolUse", message=message)]])

    with pytest.raises(RetriesExhaustedError, match="you called `advance`"):
        async for _ in run_skill_loop(
            provider=provider,
            model="m",
            skill=skill,
            observation=HarnessObservation(user_message="start"),
            max_retries=0,
        ):
            pass


@pytest.mark.asyncio
async def test_a_provider_error_is_raised_instead_of_retried_as_a_bad_step():
    """Retrying the same prompt cannot fix a provider failure.

    Reporting one as a malformed step burned the retry budget and replaced the
    provider's message with a protocol complaint the user could not act on.
    """
    from rio_ai import AssistantErrorEvent, AssistantMessage

    skill = make_skill()
    error = AssistantMessage(content=[], stop_reason="error", error_message="429 rate limited")
    provider = FakeProvider([[AssistantErrorEvent(reason="error", error=error)]])

    with pytest.raises(ProviderResponseError, match="429 rate limited"):
        async for _ in run_skill_loop(
            provider=provider,
            model="m",
            skill=skill,
            observation=HarnessObservation(user_message="start"),
        ):
            pass


@pytest.mark.asyncio
async def test_a_failing_action_becomes_an_observation_instead_of_crashing():
    """A raising tool must not end the run.

    The step's state update has already been committed when the action runs, so
    the loop is in a consistent state; the failure is reported as this step's
    observation and the model recovers from it like any other result. That is
    the whole recovery path -- there is no history to unwind.
    """
    from conftest import FINISH
    from rio_ai import AgentTool

    async def _explode(tool_call_id, arguments, signal=None, on_update=None):
        raise RuntimeError("disk is on fire")

    exploding = AgentTool(
        name="explode",
        label="Explode",
        description="Always fails.",
        parameters={"type": "object", "properties": {}},
        execute_fn=_explode,
    )
    skill = make_skill(actions=(exploding, FINISH))
    provider = FakeProvider(
        [
            step_response(reasoning="r1", state_delta={"counter": 1}, action="explode", args={}),
            step_response(reasoning="r2", state_delta={"counter": 2}, action="finish", args={}),
        ]
    )

    events = [
        event
        async for event in run_skill_loop(
            provider=provider,
            model="m",
            skill=skill,
            observation=HarnessObservation(user_message="start"),
        )
    ]

    action_ends = [e for e in events if isinstance(e, ActionEndEvent)]
    assert [e.is_error for e in action_ends] == [True, False]
    assert "disk is on fire" in action_ends[0].result.text

    # The run continued, and the failure was handed to the next step verbatim.
    _model, _system, second_messages, _tools = provider.calls[1]
    assert "disk is on fire" in second_messages[0].content

    run_end = events[-1]
    assert isinstance(run_end, RunEndEvent)
    assert run_end.state["counter"] == 2


@pytest.mark.parametrize(
    "action", ["finish", [], {"name": []}, {"name": "finish", "arguments": []}]
)
async def test_malformed_action_rolls_back_then_retries(action):
    invalid = step_response(reasoning="", state_delta={"counter": 99}, action="finish", args={})
    invalid[0].message.tool_calls[0].arguments["action"] = action
    provider = FakeProvider(
        [
            invalid,
            step_response(reasoning="", state_delta={"counter": 1}, action="finish", args={}),
        ]
    )
    events = [
        event
        async for event in run_skill_loop(
            provider=provider,
            model="m",
            skill=make_skill(),
            observation=HarnessObservation(user_message="start"),
        )
    ]
    assert len([event for event in events if isinstance(event, ValidationErrorEvent)]) == 1
    assert [event.state["counter"] for event in events if isinstance(event, StateUpdateEvent)] == [
        1
    ]


async def test_only_the_first_of_several_step_calls_is_executed():
    """A step is one action, however many `skill_step` calls arrive.

    Models emit parallel calls, and a provider can fragment one call into
    several. Rejecting the whole response spent the retry budget on something
    a retry does not fix, so the first proposal is committed and the rest
    dropped -- the model sees that action's observation next and continues.
    """
    duplicated = step_response(reasoning="", state_delta={"counter": 99}, action="advance", args={})
    duplicated[0].message.content.append(duplicated[0].message.tool_calls[0].model_copy())
    provider = FakeProvider(
        [
            duplicated,
            step_response(reasoning="", state_delta={"counter": 1}, action="finish", args={}),
        ]
    )
    events = [
        event
        async for event in run_skill_loop(
            provider=provider,
            model="m",
            skill=make_skill(),
            observation=HarnessObservation(user_message="start"),
        )
    ]
    assert not [event for event in events if isinstance(event, ValidationErrorEvent)]
    assert [event.name for event in events if isinstance(event, ActionEndEvent)] == [
        "advance",
        "finish",
    ]
    assert events[-1].state == {"counter": 1}


async def test_an_observations_inputs_are_labelled_separately():
    """A user message is not an action result: no action has run to produce one."""
    skill = make_skill()
    provider = FakeProvider(
        [
            step_response(reasoning="", state_delta={"counter": 1}, action="advance", args={}),
            step_response(reasoning="", state_delta={"counter": 2}, action="finish", args={}),
        ]
    )
    [
        event
        async for event in run_skill_loop(
            provider=provider,
            model="m",
            skill=skill,
            observation=HarnessObservation(user_message="explain main.py"),
        )
    ]

    first, second = (messages[0].content for _m, _s, messages, _t in provider.calls)
    assert "New User Message:\nexplain main.py" in first
    assert "Latest Observation:" not in first
    # The message is spent once an action has run; the observation replaces it.
    assert "New User Message:" not in second
    assert "Latest Observation:" in second


async def test_a_message_arriving_mid_run_carries_the_observation_with_it():
    skill = make_skill()
    provider = FakeProvider(
        [step_response(reasoning="", state_delta={"counter": 1}, action="finish", args={})]
    )
    [
        event
        async for event in run_skill_loop(
            provider=provider,
            model="m",
            skill=skill,
            observation=HarnessObservation(
                user_message="actually, stop", tool_call_result="advance ran"
            ),
        )
    ]

    body = provider.calls[0][2][0].content
    assert "Latest Observation:\nadvance ran" in body
    assert "New User Message:\nactually, stop" in body


async def test_an_observation_of_nothing_is_refused():
    with pytest.raises(ValueError, match="a step needs something to observe"):
        HarnessObservation()


@pytest.mark.asyncio
async def test_a_truncated_step_call_is_reported_as_truncation_not_bad_shape():
    """A call cut off at the output limit must say so, not complain about shape.

    The arguments of a step that ran out of output tokens mid-call never parse,
    so `action` is absent and the shape checks would reject it with "action must
    be a JSON object" -- a note that sent the model back to re-send the same
    oversized call until the retry budget ran out and the run died.
    """
    from rio_agent.prompt import STEP_TOOL_NAME
    from rio_ai import AssistantDoneEvent, AssistantMessage, ToolCall, malformed_tool_arguments

    skill = make_skill()
    partial = '{"state_delta": {}, "action": {"name": "advance", "arguments": {"note": "aaa'
    message = AssistantMessage(
        content=[
            ToolCall(id="call-0", name=STEP_TOOL_NAME, arguments=malformed_tool_arguments(partial))
        ],
        stop_reason="length",
    )
    provider = FakeProvider(
        [
            [AssistantDoneEvent(reason="length", message=message)],
            step_response(reasoning="smaller", state_delta={}, action="finish", args={}),
        ]
    )

    events = [
        event
        async for event in run_skill_loop(
            provider=provider,
            model="m",
            skill=skill,
            observation=HarnessObservation(user_message="start"),
            max_retries=1,
        )
    ]

    error = next(e for e in events if isinstance(e, ValidationErrorEvent)).error
    assert "output token limit" in error
    assert str(len(partial)) in error
    assert "JSON object" not in error
    assert any(isinstance(e, ActionEndEvent) and e.name == "finish" for e in events)


@pytest.mark.asyncio
async def test_unparseable_step_arguments_do_not_replay_the_same_call():
    """Malformed arguments with no truncation still get an actionable note."""
    from rio_agent.prompt import STEP_TOOL_NAME
    from rio_ai import AssistantDoneEvent, AssistantMessage, ToolCall, malformed_tool_arguments

    skill = make_skill()
    message = AssistantMessage(
        content=[
            ToolCall(id="call-0", name=STEP_TOOL_NAME, arguments=malformed_tool_arguments("{oops"))
        ],
        stop_reason="toolUse",
    )
    provider = FakeProvider([[AssistantDoneEvent(reason="toolUse", message=message)]])

    with pytest.raises(RetriesExhaustedError, match="could not be parsed"):
        async for _ in run_skill_loop(
            provider=provider,
            model="m",
            skill=skill,
            observation=HarnessObservation(user_message="start"),
            max_retries=0,
        ):
            pass


@pytest.mark.asyncio
async def test_a_state_that_outgrows_its_budget_is_rejected_before_it_is_committed():
    """The whole state ships every step, so it cannot be allowed to grow without bound."""
    skill = make_skill(state_budget_chars=300)
    provider = FakeProvider(
        [
            step_response(
                reasoning="", state_delta={"notes": "x" * 500}, action="advance", args={}
            ),
            step_response(reasoning="", state_delta={"notes": "small"}, action="finish", args={}),
        ]
    )

    events = [
        event
        async for event in run_skill_loop(
            provider=provider,
            model="m",
            skill=skill,
            observation=HarnessObservation(user_message="start"),
        )
    ]

    error = next(e for e in events if isinstance(e, ValidationErrorEvent)).error
    assert "over the 300 limit" in error
    assert events[-1].state["notes"] == "small"


async def test_action_executor_records_results_and_reports_failures():
    async def execute(action, call_id, arguments, state, signal):
        if arguments.get("note") == "refuse":
            raise ValueError("advance refused")
        result = await action.execute(call_id, arguments, signal)
        return result, {"notes": result.text}

    provider = FakeProvider(
        [
            step_response(reasoning="", state_delta={}, action="advance", args={"note": "a"}),
            step_response(reasoning="", state_delta={}, action="advance", args={"note": "refuse"}),
            step_response(reasoning="", state_delta={}, action="finish", args={}),
        ]
    )
    events = [
        event
        async for event in run_skill_loop(
            provider=provider,
            model="m",
            skill=make_skill(execute_action=execute),
            observation=HarnessObservation(user_message="start"),
        )
    ]
    assert "observed:a" in provider.calls[1][2][0].content.partition("Latest Observation:")[0]
    assert "advance refused" in provider.calls[2][2][0].content
    assert [e.is_error for e in events if isinstance(e, ActionEndEvent)] == [False, True, False]
    assert events[-1].state["notes"] == "terminal"
