"""The SKILL.state execution loop.

At each step, the runtime sends the model only three things: the skill
instructions, the current state, and the latest observation -- the result the
last action produced, a message from the user, or, when a message arrives
mid-run, both. It never sends a growing transcript. The model must respond
with one `skill_step` tool call carrying its private reasoning, a state
update, and an action. The runtime checks the state update and the action --
including whether the updated state still fits the skill's state budget --
and an invalid proposal triggers a rollback-retry cycle, bounded by
`max_retries`, instead of being committed. On success, the runtime commits
the new state, discards the reasoning for good, runs the action to get the
next observation, and the loop repeats.

A skill may supply an `ActionObserver`. It sees each action before it runs,
so it can refuse one the state says is unsafe, and again after it returns,
so what the action produced -- which the model could not know when it wrote
its delta -- is recorded in the state rather than left in an observation
that is discarded one step later.

Because the prompt never includes history, total prompt size across a run
grows in proportion to the number of steps, not the square of it -- see
`tests/test_rio_agent_loop.py::test_prompt_footprint_is_bounded_across_steps`
for a runtime check of that property.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from rio_agent.errors import (
    ActionNotFoundError,
    ProviderResponseError,
    RetriesExhaustedError,
    StateValidationError,
)
from rio_agent.events import (
    ActionEndEvent,
    ActionStartEvent,
    ReasoningDiscardedEvent,
    RunEndEvent,
    RunStartEvent,
    SkillEvent,
    StateUpdateEvent,
    StepEndEvent,
    StepStartEvent,
    ValidationErrorEvent,
)
from rio_agent.observation import HarnessObservation
from rio_agent.prompt import STEP_TOOL_NAME, build_step_messages, skill_step_tool
from rio_agent.skill import HarnessSpec
from rio_agent.state import apply_state_delta, check_state_budget, validate_state_delta
from rio_ai.messages import AssistantMessage, TextContent, raw_tool_arguments
from rio_ai.provider import CancellationToken, ModelProvider
from rio_ai.provider_events import AssistantDoneEvent, AssistantErrorEvent
from rio_ai.tools import AgentToolResult


async def run_skill_loop(
    *,
    provider: ModelProvider,
    model: str,
    skill: HarnessSpec,
    observation: HarnessObservation,
    state: dict | None = None,
    max_steps: int | None = None,
    max_retries: int = 2,
    signal: CancellationToken | None = None,
) -> AsyncIterator[SkillEvent]:
    """Run the SKILL.state loop, yielding one event per lifecycle transition.

    `observation` is the first step's `O_0`. A turn started by the user
    carries only their message; a run steered mid-flight carries the message
    and the result the run had reached. Every step after the first observes
    the result of the action the previous one took.
    """
    state = dict(state if state is not None else skill.initial_state)
    actions = skill.action_by_name()
    tool = skill_step_tool(skill)

    yield RunStartEvent(skill=skill.name)

    step = 0
    current_observation = observation
    terminated = False
    while max_steps is None or step < max_steps:
        if signal is not None and signal.is_cancelled():
            break

        yield StepStartEvent(step=step, state=dict(state), observation=current_observation)

        delta, action_name, action_arguments = None, None, None
        error_note: str | None = None
        attempt = 0
        while True:
            messages = build_step_messages(state, current_observation, error_note=error_note)
            assistant = await _call_model(
                provider, model, skill.instructions, messages, [tool], signal
            )
            if assistant.stop_reason == "error":
                # A provider failure is not a malformed step: retrying the same
                # prompt cannot fix it, and reporting it as one hides the real
                # message behind a protocol complaint.
                raise ProviderResponseError(assistant.error_message or "provider returned an error")

            # A step is one action, so only the first `skill_step` call is used.
            # Models that emit several in parallel are not retried: the first
            # proposal is committed and its observation is what they see next.
            call = next(
                (c for c in assistant.tool_calls if c.name == STEP_TOOL_NAME),
                None,
            )
            if call is None:
                called = ", ".join(f"`{c.name}`" for c in assistant.tool_calls) or "no tool"
                error_note = (
                    f"you must call the `{STEP_TOOL_NAME}` tool; you called {called}. "
                    f"Every action goes in that call's `action` field."
                )
                attempt += 1
                yield ValidationErrorEvent(step=step, attempt=attempt, error=error_note)
                if attempt > max_retries:
                    raise RetriesExhaustedError(error_note)
                continue

            # Arguments that never parsed reach here as the raw text. Retrying
            # on the shape complaint the checks below would raise ("action must
            # be a JSON object") sent the model back to write the same oversized
            # call again, so say what actually went wrong instead.
            raw_arguments = raw_tool_arguments(call.arguments)
            if raw_arguments is not None:
                error_note = _truncated_call_note(
                    raw_arguments, truncated=assistant.stop_reason == "length"
                )
                attempt += 1
                yield ValidationErrorEvent(step=step, attempt=attempt, error=error_note)
                if attempt > max_retries:
                    raise RetriesExhaustedError(error_note)
                continue

            if assistant.text:
                yield ReasoningDiscardedEvent(step=step, reasoning=assistant.text)

            proposed_delta = call.arguments.get("state_delta")
            proposed_action = call.arguments.get("action")
            try:
                if not isinstance(proposed_delta, dict):
                    raise StateValidationError(
                        f"state_delta must be a JSON object, got {type(proposed_delta).__name__}"
                    )
                validate_state_delta(proposed_delta, allowed_fields=skill.state_fields)
                if not isinstance(proposed_action, dict):
                    raise StateValidationError("action must be a JSON object")
                candidate_name = proposed_action.get("name")
                if not isinstance(candidate_name, str) or candidate_name not in actions:
                    raise ActionNotFoundError(
                        f"unknown action {candidate_name!r}; declared actions are {sorted(actions)}"
                    )
                candidate_arguments = proposed_action.get("arguments", {})
                if not isinstance(candidate_arguments, dict):
                    raise StateValidationError("action arguments must be a JSON object")
                candidate_state = apply_state_delta(state, proposed_delta)
                check_state_budget(candidate_state, max_chars=skill.state_budget_chars)
            except (StateValidationError, ActionNotFoundError) as exc:
                error_note = str(exc)
                attempt += 1
                yield ValidationErrorEvent(step=step, attempt=attempt, error=error_note)
                if attempt > max_retries:
                    raise RetriesExhaustedError(error_note) from exc
                continue

            delta = proposed_delta
            action_name = candidate_name
            action_arguments = dict(candidate_arguments)
            break

        state = candidate_state
        yield StateUpdateEvent(step=step, delta=dict(delta), state=dict(state))

        yield ActionStartEvent(step=step, name=action_name, arguments=dict(action_arguments))
        try:
            if skill.observer is not None:
                skill.observer.before_action(state, action_name, action_arguments)
            result = await actions[action_name].execute(
                f"{skill.name}-step-{step}", action_arguments, signal
            )
            is_error = False
        except Exception as exc:
            # A failing action is an observation, not a crash. The state update
            # for this step has already been committed, so the model resumes
            # from a consistent state with the failure as its latest
            # observation -- the same recovery path as any other result.
            result = AgentToolResult(content=[TextContent(text=f"{action_name} failed: {exc}")])
            is_error = True
        yield ActionEndEvent(step=step, name=action_name, result=result, is_error=is_error)

        # What the action actually produced is knowledge the model did not have
        # when it wrote its own delta, so the observer records it afterwards.
        note: str | None = None
        if skill.observer is not None and not is_error:
            outcome = skill.observer.after_action(state, action_name, action_arguments, result)
            note = outcome.note
            if outcome.delta:
                state = apply_state_delta(state, outcome.delta)
                yield StateUpdateEvent(step=step, delta=dict(outcome.delta), state=dict(state))

        terminated = bool(result.terminate)
        yield StepEndEvent(step=step, state=dict(state), terminated=terminated)

        step += 1
        if terminated:
            break
        observed = result.text or "(no observation)"
        if note:
            observed += f"\n\n[{note}]"
        current_observation = HarnessObservation(tool_call_result=observed)

    yield RunEndEvent(steps=step, state=dict(state))


def _truncated_call_note(raw_arguments: str, *, truncated: bool) -> str:
    """Explain unparseable step arguments in terms the next attempt can act on."""
    cause = (
        "the response hit the output token limit before the call was finished"
        if truncated
        else "the arguments were not valid JSON"
    )
    return (
        f"your `{STEP_TOOL_NAME}` arguments could not be parsed -- {cause} "
        f"({len(raw_arguments)} characters were received). Retry with a smaller "
        "action: write or edit the file in several steps instead of sending its "
        "whole content in one call."
    )


async def _call_model(
    provider: ModelProvider,
    model: str,
    system: str,
    messages: list,
    tools: list,
    signal: CancellationToken | None,
) -> AssistantMessage:
    result: AssistantMessage | None = None
    async for event in provider.stream_response(
        model=model, system=system, messages=messages, tools=tools, signal=signal
    ):
        if isinstance(event, AssistantDoneEvent):
            result = event.message
        elif isinstance(event, AssistantErrorEvent):
            result = event.error
    if result is None:
        raise RuntimeError("provider produced no assistant message")
    return result
