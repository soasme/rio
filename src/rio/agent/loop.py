"""The Context Language Model (CLM) loop.

The model manages its own context (arXiv:2609.37725). Before every step the
runtime writes the context to a file. The model replies with text and one tool
call; the runtime runs the tool, then reads the file back. If the model edited
it, the edited turns replace the context, provided they fit the limit. The
step's reply and observation are appended, and the loop repeats until an
action terminates the run.

Validity is the runtime's job; strategy is the model's. The runtime never
summarizes. It only reports the context size after every observation and,
when the model lets the context outgrow its limit, withholds the oldest
observations so the next request still fits.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

from rio.agent.context import (
    Turn,
    context_tokens,
    estimate_tokens,
    parse_context,
    read_context,
    turn,
    withhold_oldest,
    write_context,
)
from rio.agent.errors import ProviderResponseError, RetriesExhaustedError
from rio.agent.events import (
    ActionEndEvent,
    ActionStartEvent,
    AgentEvent,
    ContextEditEvent,
    ReasoningEvent,
    RunEndEvent,
    RunStartEvent,
    StepEndEvent,
    StepStartEvent,
    ValidationErrorEvent,
)
from rio.agent.prompt import build_messages, context_protocol
from rio.agent.skill import HarnessSpec
from rio.ai.messages import AssistantMessage, TextContent, ToolCall, raw_tool_arguments
from rio.ai.provider import CancellationToken, ModelProvider
from rio.ai.provider_events import AssistantDoneEvent, AssistantErrorEvent
from rio.ai.tools import AgentToolResult

#: The share of the context window the context may use; the rest is headroom
#: for the system prompt, tool definitions, and the reply.
CONTEXT_LIMIT_RATIO = 0.8
#: The share of the limit at which each observation asks the model to compact.
COMPACT_HINT_RATIO = 0.75
DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000


def context_limit(context_window_tokens: int) -> int:
    if context_window_tokens <= 0:
        raise ValueError("context_window_tokens must be positive")
    return int(context_window_tokens * CONTEXT_LIMIT_RATIO)


def default_context_file() -> Path:
    """Return a fresh private context file path."""
    return Path(tempfile.mkdtemp(prefix="rio-context-")) / "CONTEXT.md"


async def run_context_loop(
    *,
    provider: ModelProvider,
    model: str,
    skill: HarnessSpec,
    observation: str | None = None,
    context: list[Turn] | None = None,
    context_file: Path | None = None,
    max_steps: int | None = None,
    max_retries: int = 2,
    context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS,
    signal: CancellationToken | None = None,
) -> AsyncIterator[AgentEvent]:
    """Run the CLM loop, yielding one event per lifecycle transition.

    ``observation`` is appended to ``context`` as a user turn before the first step.
    """
    path = context_file or default_context_file()
    limit = context_limit(context_window_tokens)
    system = skill.instructions + "\n\n" + context_protocol(path, limit)
    actions = skill.action_by_name()
    tools = list(skill.actions)
    context = [dict(item) for item in context or []]
    if observation is not None:
        context.append(turn("user", observation))

    yield RunStartEvent(skill=skill.name)

    step = 0
    while max_steps is None or step < max_steps:
        if signal is not None and signal.is_cancelled():
            break

        over = context_tokens(context) - limit
        if over > 0:
            context = withhold_oldest(context, over_tokens=over)
        yield StepStartEvent(step=step, context=[dict(item) for item in context])

        error_note: str | None = None
        attempt = 0
        while True:
            rendered = write_context(path, context)
            assistant = await _call_model(
                provider,
                model,
                system,
                build_messages(context, error_note=error_note),
                tools,
                signal,
            )
            if assistant.stop_reason == "error":
                raise ProviderResponseError(assistant.error_message or "provider returned an error")
            call = assistant.tool_calls[0] if assistant.tool_calls else None
            error_note = _call_error(assistant, call, actions)
            if error_note is None:
                break
            attempt += 1
            yield ValidationErrorEvent(step=step, attempt=attempt, error=error_note)
            if attempt > max_retries:
                raise RetriesExhaustedError(error_note)

        assert call is not None
        if assistant.text:
            yield ReasoningEvent(step=step, reasoning=assistant.text)
        arguments = dict(call.arguments)
        yield ActionStartEvent(step=step, name=call.name, arguments=dict(arguments))
        try:
            result = await actions[call.name].execute(
                f"{skill.name}-step-{step}", arguments, signal
            )
            is_error = False
        except Exception as exc:
            # A failing action is an observation, not a crash.
            result = AgentToolResult(content=[TextContent(text=f"{call.name} failed: {exc}")])
            is_error = True
        yield ActionEndEvent(step=step, name=call.name, result=result, is_error=is_error)

        edited = read_context(path)
        note = ""
        if edited is not None and edited.strip() != rendered.strip():
            candidate = parse_context(edited)
            before, after = context_tokens(context), context_tokens(candidate)
            accepted = after <= limit
            if accepted:
                context = candidate
                note = (
                    f"\n[context edit applied: ~{before} -> ~{after} tokens, {len(context)} turns]"
                )
            else:
                note = (
                    f"\n[context edit rejected: ~{after} tokens is over the ~{limit}-token limit. "
                    "Replace stale text with shorter summaries.]"
                )
            yield ContextEditEvent(
                step=step,
                accepted=accepted,
                before_tokens=before,
                after_tokens=after,
                turns=len(candidate),
            )

        reply = "\n".join(
            part for part in (assistant.text, f"{call.name} {_json(arguments)}") if part
        )
        if len(assistant.tool_calls) > 1:
            note += "\n[only the first tool call ran: call one tool per reply]"
        context.append(turn("assistant", reply))
        observation_text = (result.text or "(no output)") + note
        context.append(turn("tool", observation_text + _readout(context, limit, path)))

        terminated = bool(result.terminate)
        yield StepEndEvent(
            step=step, context=[dict(item) for item in context], terminated=terminated
        )
        step += 1
        if terminated:
            break

    yield RunEndEvent(steps=step, context=[dict(item) for item in context])


def _call_error(assistant: AssistantMessage, call: ToolCall | None, actions: dict) -> str | None:
    """Return why a reply has no usable action, or None when it has one."""
    if call is None:
        return f"call exactly one tool; declared tools are {sorted(actions)}."
    raw_arguments = raw_tool_arguments(call.arguments)
    if raw_arguments is not None:
        cause = (
            "the response hit the output token limit before the call was finished"
            if assistant.stop_reason == "length"
            else "the arguments were not valid JSON"
        )
        return (
            f"your `{call.name}` arguments could not be parsed -- {cause} "
            f"({len(raw_arguments)} characters were received). Retry with a smaller "
            "action: write or edit the file in several steps instead of sending its "
            "whole content in one call."
        )
    if call.name not in actions:
        return f"unknown tool {call.name!r}; declared tools are {sorted(actions)}."
    return None


def _readout(context: list[Turn], limit: int, path: Path) -> str:
    tokens = context_tokens(context) + estimate_tokens("\n[context: ~000000/000000 tokens]")
    hint = f" -- compact {path} now" if tokens >= limit * COMPACT_HINT_RATIO else ""
    return f"\n[context: ~{tokens}/{limit} tokens{hint}]"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


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
