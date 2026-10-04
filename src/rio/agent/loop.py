"""Provider loop over a JSON object state."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from rio.agent.errors import ProviderResponseError, RetriesExhaustedError
from rio.agent.prompt import DEFAULT_SYSTEM_PROMPT, STEP_TOOL_NAME, build_messages, skill_step_tool
from rio.agent.state import PatchError, apply_patch
from rio.ai.messages import AssistantMessage, raw_tool_arguments
from rio.ai.provider import CancellationToken, ModelProvider
from rio.ai.provider_events import AssistantDoneEvent, AssistantErrorEvent
from rio.ai.types import JSONObject


@dataclass(frozen=True, slots=True)
class AgentStep:
    state: JSONObject
    patch: list[JSONObject]
    reply: str | None


async def run_json_loop(
    *,
    provider: ModelProvider,
    model: str,
    state: JSONObject,
    instructions: str = "",
    max_steps: int | None = None,
    max_retries: int = 2,
    signal: CancellationToken | None = None,
) -> AsyncIterator[AgentStep]:
    """Yield each accepted patch; callers decide how to persist or interpret state."""
    system = DEFAULT_SYSTEM_PROMPT + ("\n\n" + instructions if instructions else "")
    tool = skill_step_tool()
    step = 0
    while max_steps is None or step < max_steps:
        if signal is not None and signal.is_cancelled():
            break
        error_note = None
        for attempt in range(max_retries + 1):
            assistant: AssistantMessage | None = None
            async for event in provider.stream_response(
                model=model,
                system=system,
                messages=build_messages(state, error_note=error_note),
                tools=[tool],
                signal=signal,
            ):
                if isinstance(event, AssistantDoneEvent):
                    assistant = event.message
                elif isinstance(event, AssistantErrorEvent):
                    assistant = event.error
            if assistant is None:
                raise RuntimeError("provider produced no assistant message")
            if assistant.stop_reason == "error":
                raise ProviderResponseError(assistant.error_message or "provider returned an error")
            call = next((c for c in assistant.tool_calls if c.name == STEP_TOOL_NAME), None)
            if call is None:
                error_note = f"call the `{STEP_TOOL_NAME}` tool"
            elif raw_tool_arguments(call.arguments) is not None:
                error_note = "tool arguments were not valid JSON"
            else:
                try:
                    state = apply_patch(state, call.arguments.get("patch"))
                except PatchError as exc:
                    error_note = str(exc)
                else:
                    break
            if attempt == max_retries:
                raise RetriesExhaustedError(error_note)
        assert call is not None
        reply = call.arguments.get("reply")
        reply = reply if isinstance(reply, str) and reply.strip() else None
        yield AgentStep(state=state, patch=call.arguments["patch"], reply=reply)
        step += 1
        if reply is not None:
            break
