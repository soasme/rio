"""Provider contract owned by Rio's portable agent layer."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Literal, Protocol

from pydantic import Field

from rio.ai._provider_events import (
    ProviderErrorEvent,
    ProviderEvent,
    ProviderResponseEndEvent,
    ProviderResponseStartEvent,
    ProviderRetryEvent,
    ProviderTextDeltaEvent,
    ProviderThinkingDeltaEvent,
    ProviderToolCallEvent,
)
from rio.ai.messages import (
    AgentMessage,
    AssistantMessage,
    AssistantMessageDiagnostic,
    TextContent,
    ThinkingContent,
    WireModel,
)
from rio.ai.tools import AgentTool

DoneReason = Literal["stop", "length", "toolUse"]
ErrorReason = Literal["error"]


class AssistantDoneEvent(WireModel):
    type: Literal["done"] = "done"
    reason: DoneReason
    message: AssistantMessage


class AssistantErrorEvent(WireModel):
    type: Literal["error"] = "error"
    reason: ErrorReason
    error: AssistantMessage


type AssistantMessageEvent = Annotated[
    AssistantDoneEvent | AssistantErrorEvent, Field(discriminator="type")
]


class ModelProvider(Protocol):
    """Provider-neutral interface for final assistant results."""

    def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
    ) -> AsyncIterator[AssistantMessageEvent]:
        """Return the final assistant message or a provider error."""
        ...


class ClosableModelProvider(ModelProvider, Protocol):
    """Model provider that owns resources released by ``aclose``."""

    async def aclose(self) -> None:
        """Close any provider-owned resources."""
        ...


async def collect_response(
    source: AsyncIterator[ProviderEvent], *, api: str, provider: str, model: str
) -> AsyncIterator[AssistantMessageEvent]:
    """Collect ordered blocks once, without exposing or copying partial messages."""
    partial = AssistantMessage(api=api, provider=provider, model=model)
    fragments: list[str] = []
    kind: str | None = None
    result: AssistantMessageEvent | None = None

    def flush() -> None:
        if fragments:
            text = "".join(fragments)
            partial.content.append(
                TextContent(text=text) if kind == "text" else ThinkingContent(thinking=text)
            )
            fragments.clear()

    async for event in source:
        if result is not None:
            continue
        if isinstance(event, ProviderResponseStartEvent):
            partial.response_provider = event.response_provider
        elif isinstance(event, ProviderRetryEvent):
            # A discarded attempt must not leak reasoning into the next response.
            partial.content.clear()
            fragments.clear()
            kind = None
        elif isinstance(event, (ProviderTextDeltaEvent, ProviderThinkingDeltaEvent)):
            next_kind = "text" if isinstance(event, ProviderTextDeltaEvent) else "thinking"
            if next_kind != kind:
                flush()
                kind = next_kind
            fragments.append(event.delta)
        elif isinstance(event, ProviderToolCallEvent):
            flush()
            partial.content.append(event.tool_call)
            kind = None
        elif isinstance(event, ProviderResponseEndEvent):
            flush()
            final = event.message
            final.api, final.provider, final.model = api, provider, model
            final.response_provider = final.response_provider or partial.response_provider
            if partial.content:
                _copy_replay_metadata(partial, final)
                final.content = partial.content
            final.stop_reason = _finish_reason(
                event.finish_reason, has_tools=bool(final.tool_calls)
            )
            result = AssistantDoneEvent(reason=final.stop_reason, message=final)
        elif isinstance(event, ProviderErrorEvent):
            flush()
            partial.response_provider = event.response_provider or partial.response_provider
            partial.stop_reason = "error"
            partial.error_message = event.message
            partial.diagnostics = [
                AssistantMessageDiagnostic(type="provider_error", details=event.data)
            ]
            result = AssistantErrorEvent(reason="error", error=partial)
    if result is None:
        flush()
        partial.stop_reason = "error"
        partial.error_message = "Provider stream ended without a terminal event"
        result = AssistantErrorEvent(reason="error", error=partial)
    yield result


def _copy_replay_metadata(target: AssistantMessage, source: AssistantMessage) -> None:
    """Copy provider metadata onto streamed blocks without changing their order."""
    source_thinking = [block for block in source.content if isinstance(block, ThinkingContent)]
    target_thinking = [block for block in target.content if isinstance(block, ThinkingContent)]
    for target_block, source_block in zip(target_thinking, source_thinking, strict=False):
        target_block.thinking_signature = source_block.thinking_signature
        target_block.redacted = source_block.redacted

    source_text = [block for block in source.content if isinstance(block, TextContent)]
    target_text = [block for block in target.content if isinstance(block, TextContent)]
    for target_text_block, source_text_block in zip(target_text, source_text, strict=False):
        target_text_block.text_signature = source_text_block.text_signature


def _finish_reason(value: str | None, *, has_tools: bool) -> DoneReason:
    # A truncated tool call must not hide the output-limit stop reason.
    if value in {"length", "max_tokens", "MAX_TOKENS", "incomplete"}:
        return "length"
    if has_tools or value in {"tool_calls", "tool_use", "toolUse"}:
        return "toolUse"
    return "stop"
