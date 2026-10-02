"""The notebook loop: a Context Language Model whose context is a Jupyter notebook.

The model manages its own context (arXiv:2609.37725), and the context is a
runnable notebook rather than a transcript. Each step the model sees the whole
notebook and replies with one `skill_step` call carrying a JSON Patch. The
runtime applies it, rejects it through the retry path if the result is not a
valid notebook or outgrows the limit, then runs the changed code cells. The
notebook with their outputs is the next step's context. A step that sets
`reply` ends the run.

Validity is the runtime's job; strategy is the model's. The runtime never
summarizes or drops cells. It only caps each new output's size.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from rio.agent.errors import ProviderResponseError, RetriesExhaustedError
from rio.agent.events import (
    AgentEvent,
    ExecutionEvent,
    PatchEvent,
    ReasoningEvent,
    RunEndEvent,
    RunStartEvent,
    StepEndEvent,
    StepStartEvent,
    ValidationErrorEvent,
)
from rio.agent.notebook import (
    Notebook,
    NotebookError,
    append_cell,
    apply_patch,
    changed_cells,
    error_output,
    markdown_cell,
    new_notebook,
    notebook_tokens,
)
from rio.agent.prompt import STEP_TOOL_NAME, build_messages, notebook_protocol, skill_step_tool
from rio.agent.skill import HarnessSpec
from rio.ai.messages import AssistantMessage, ToolCall, raw_tool_arguments
from rio.ai.provider import CancellationToken, ModelProvider
from rio.ai.provider_events import AssistantDoneEvent, AssistantErrorEvent
from rio.ai.types import JSONObject

#: The share of the context window the notebook may use; the rest is headroom
#: for the system prompt, the tool definition, and the reply.
CONTEXT_LIMIT_RATIO = 0.8
DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000


def context_limit(context_window_tokens: int) -> int:
    if context_window_tokens <= 0:
        raise ValueError("context_window_tokens must be positive")
    return int(context_window_tokens * CONTEXT_LIMIT_RATIO)


async def run_notebook_loop(
    *,
    provider: ModelProvider,
    model: str,
    skill: HarnessSpec,
    observation: str | None = None,
    notebook: Notebook | None = None,
    max_steps: int | None = None,
    max_retries: int = 2,
    context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS,
    signal: CancellationToken | None = None,
) -> AsyncIterator[AgentEvent]:
    """Run the notebook loop, yielding one event per lifecycle transition.

    ``observation`` is appended to ``notebook`` as a user markdown cell before the first step.
    """
    limit = context_limit(context_window_tokens)
    system = skill.instructions + "\n\n" + notebook_protocol(limit)
    tool = skill_step_tool()
    notebook = notebook if notebook is not None else new_notebook()
    if observation is not None:
        notebook = append_cell(notebook, markdown_cell(observation, role="user"))

    yield RunStartEvent(skill=skill.name)

    step = 0
    reply: str | None = None
    while max_steps is None or step < max_steps:
        if signal is not None and signal.is_cancelled():
            break
        yield StepStartEvent(step=step, notebook=notebook)

        error_note: str | None = None
        attempt = 0
        while True:
            assistant = await _call_model(
                provider,
                model,
                system,
                build_messages(notebook, limit_tokens=limit, error_note=error_note),
                [tool],
                signal,
            )
            if assistant.stop_reason == "error":
                raise ProviderResponseError(assistant.error_message or "provider returned an error")
            call = next((c for c in assistant.tool_calls if c.name == STEP_TOOL_NAME), None)
            patched, error_note = _check(assistant, call, notebook, limit)
            if error_note is None:
                break
            attempt += 1
            yield ValidationErrorEvent(step=step, attempt=attempt, error=error_note)
            if attempt > max_retries:
                raise RetriesExhaustedError(error_note)

        assert call is not None and patched is not None
        if assistant.text:
            yield ReasoningEvent(step=step, reasoning=assistant.text)
        patch: list[JSONObject] = list(call.arguments["patch"])  # type: ignore[arg-type]
        cells = changed_cells(notebook, patched)
        yield PatchEvent(step=step, patch=patch, cells=cells)
        if cells:
            try:
                patched = await skill.executor(patched, cells)
            except Exception as exc:
                # A kernel that fails to start is an observation, not a crash.
                patched["cells"][cells[0]]["outputs"] = [error_output(exc)]  # type: ignore[index]
            yield ExecutionEvent(step=step, cells=cells, notebook=patched)
        notebook = patched

        reply = call.arguments.get("reply")  # type: ignore[assignment]
        if not isinstance(reply, str):
            reply = None
        if reply is not None:
            notebook = append_cell(notebook, markdown_cell(reply, role="assistant"))
        yield StepEndEvent(step=step, notebook=notebook, reply=reply)
        step += 1
        if reply is not None:
            break

    yield RunEndEvent(steps=step, notebook=notebook, reply=reply)


def _check(
    assistant: AssistantMessage, call: ToolCall | None, notebook: Notebook, limit: int
) -> tuple[Notebook | None, str | None]:
    """Return the patched notebook, or why the reply has no usable patch."""
    if call is None:
        called = ", ".join(f"`{c.name}`" for c in assistant.tool_calls) or "no tool"
        return None, f"call the `{STEP_TOOL_NAME}` tool; you called {called}."
    raw_arguments = raw_tool_arguments(call.arguments)
    if raw_arguments is not None:
        cause = (
            "the response hit the output token limit before the call was finished"
            if assistant.stop_reason == "length"
            else "the arguments were not valid JSON"
        )
        return None, (
            f"your `{STEP_TOOL_NAME}` arguments could not be parsed -- {cause} "
            f"({len(raw_arguments)} characters were received). Retry with a smaller patch."
        )
    try:
        patched = apply_patch(notebook, call.arguments.get("patch"))
    except NotebookError as exc:
        return None, str(exc)
    size = notebook_tokens(patched)
    if size > limit and size >= notebook_tokens(notebook):
        return None, (
            f"the patched notebook would be ~{size} tokens, over the ~{limit}-token limit. "
            "Remove stale outputs and cells, keeping short notes of what you still need."
        )
    return patched, None


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
