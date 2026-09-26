"""The SKILL.state execution loop.

The runtime materializes state from accepted patches.  The model receives an
append-only history of those patches and action observations, like a
traditional agent transcript without replaying private reasoning.  When that
history reaches 80% of the context window, the runtime replaces it with one
materialized state rebuild and continues appending from there.

The model must respond
with one `skill_step` tool call carrying its private reasoning, a state
update, and an action. The runtime checks the state update and the action --
including whether the updated state still fits the skill's state budget --
and an invalid proposal triggers a rollback-retry cycle, bounded by
`max_retries`, instead of being committed. On success, the runtime commits
the new state, discards the reasoning for good, runs the action to get the
next observation, and the loop repeats.

Because the prompt never includes history, total prompt size across a run
grows in proportion to the number of steps, not the square of it -- see
`tests/test_rio_agent_loop.py::test_prompt_footprint_is_bounded_across_steps`
for a runtime check of that property.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from rio.agent.errors import (
    ActionNotFoundError,
    ProviderResponseError,
    RetriesExhaustedError,
    StateValidationError,
)
from rio.agent.events import (
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
from rio.agent.prompt import (
    STEP_TOOL_NAME,
    build_step_messages,
    observation_message,
    skill_step_tool,
    state_message,
    state_patch_message,
)
from rio.agent.skill import HarnessSpec
from rio.agent.state import apply_state_delta, check_state_budget, validate_state_delta
from rio.ai.messages import (
    AgentMessage,
    AssistantMessage,
    TextContent,
    message_text,
    raw_tool_arguments,
)
from rio.ai.provider import CancellationToken, ModelProvider
from rio.ai.provider_events import AssistantDoneEvent, AssistantErrorEvent
from rio.ai.tools import AgentTool, AgentToolResult

COMPACTION_THRESHOLD = 0.8
DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000


async def run_skill_loop(
    *,
    provider: ModelProvider,
    model: str,
    skill: HarnessSpec,
    observation: str | None = None,
    state: dict | None = None,
    max_steps: int | None = None,
    max_retries: int = 2,
    context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS,
    signal: CancellationToken | None = None,
) -> AsyncIterator[SkillEvent]:
    """Run the SKILL.state loop, yielding one event per lifecycle transition.

    ``observation`` seeds the append-only history for compatibility with the
    public low-level API. Coding sessions pass the user's task here.
    """
    state = dict(state if state is not None else skill.initial_state)
    actions = skill.action_by_name()
    tool = skill_step_tool(skill)

    yield RunStartEvent(skill=skill.name)

    step = 0
    history: list[AgentMessage] = [state_message(state)]
    if observation is not None:
        history.append(observation_message(observation))
    terminated = False
    while max_steps is None or step < max_steps:
        if signal is not None and signal.is_cancelled():
            break

        yield StepStartEvent(step=step, state=dict(state), observation=None)

        delta, action_name, action_arguments = None, None, None
        error_note: str | None = None
        attempt = 0
        while True:
            if _needs_compaction(history, skill.instructions, tool, context_window_tokens):
                # A rebuild is deliberately computed by the runtime: it is the
                # exact state obtained by applying every accepted patch, not an
                # LLM summary that may lose a fact. The state budget reserves
                # room for this single record; an agent that needs more room
                # must intentionally delete or shorten state fields in a patch.
                history = [state_message(state, rebuilt=True)]
                if _needs_compaction(history, skill.instructions, tool, context_window_tokens):
                    history = [state_message(state, rebuilt=True, needs_refresh=True)]
            messages = build_step_messages(history, error_note=error_note)
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
        history.append(state_patch_message(delta))
        yield StateUpdateEvent(step=step, delta=dict(delta), state=dict(state))

        yield ActionStartEvent(step=step, name=action_name, arguments=dict(action_arguments))
        action_delta = {}
        try:
            action = actions[action_name]
            call_id = f"{skill.name}-step-{step}"
            if skill.execute_action is None:
                result = await action.execute(call_id, action_arguments, signal)
            else:
                result, action_delta = await skill.execute_action(
                    action, call_id, action_arguments, state, signal
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

        if action_delta:
            state = apply_state_delta(state, action_delta)
            history.append(state_patch_message(action_delta))
            yield StateUpdateEvent(step=step, delta=action_delta, state=dict(state))

        terminated = bool(result.terminate)
        yield StepEndEvent(step=step, state=dict(state), terminated=terminated)

        step += 1
        if terminated:
            break
        history.append(observation_message(result.text or "(no observation)"))

    yield RunEndEvent(steps=step, state=dict(state))


def _needs_compaction(
    history: list[AgentMessage], instructions: str, tool: AgentTool, context_window_tokens: int
) -> bool:
    """Whether the next normal request would exceed the history budget."""
    if context_window_tokens <= 0:
        raise ValueError("context_window_tokens must be positive")
    # Keep this estimator local to the runtime so `rio.agent` remains usable
    # without importing the coding domain (and creating a dependency cycle).
    tokens = _estimate_tokens(instructions)
    tokens += sum(4 + _estimate_tokens(message_text(message)) for message in history)
    tokens += 16 + _estimate_tokens(tool.name) + _estimate_tokens(tool.description)
    tokens += _estimate_tokens(str(tool.input_schema))
    return tokens > context_window_tokens * COMPACTION_THRESHOLD


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4) if text else 0


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
