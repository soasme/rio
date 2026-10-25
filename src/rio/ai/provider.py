"""Provider contract owned by Rio's portable agent layer."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Literal, Protocol

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
    AssistantMessage,
    AssistantMessageDiagnostic,
    TextContent,
    ThinkingContent,
    UserMessage,
)
from rio.ai.tools import AgentTool

DoneReason = Literal["stop", "length", "toolUse"]


class ModelProvider(Protocol):
    """Provider-neutral interface: one request, one final assistant message."""

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[UserMessage],
        tools: list[AgentTool],
        on_event: Callable[[ProviderEvent], None] | None = None,
    ) -> AssistantMessage:
        """Return the response; a provider failure has ``stop_reason`` "error".

        ``on_event`` observes stream events as they arrive, up to the terminal one.
        """
        ...


class ClosableModelProvider(ModelProvider, Protocol):
    """Model provider that owns resources released by ``aclose``."""

    async def aclose(self) -> None:
        """Close any provider-owned resources."""
        ...


async def collect_response(
    source: AsyncIterator[ProviderEvent],
    *,
    api: str,
    provider: str,
    model: str,
    on_event: Callable[[ProviderEvent], None] | None = None,
) -> AssistantMessage:
    """Collect ordered blocks once, without exposing or copying partial messages.

    The source is read to its end, which is what releases the provider's transport;
    events after the first terminal one are ignored.
    """
    partial = AssistantMessage(api=api, provider=provider, model=model)
    fragments: list[str] = []
    kind: str | None = None
    result: AssistantMessage | None = None

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
        if on_event is not None:
            on_event(event)
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
                final.content = partial.content
            final.stop_reason = _finish_reason(
                event.finish_reason, has_tools=bool(final.tool_calls)
            )
            result = final
        elif isinstance(event, ProviderErrorEvent):
            flush()
            partial.response_provider = event.response_provider or partial.response_provider
            partial.stop_reason = "error"
            partial.error_message = event.message
            partial.diagnostics = [
                AssistantMessageDiagnostic(type="provider_error", details=event.data)
            ]
            result = partial
    if result is not None:
        return result
    flush()
    partial.stop_reason = "error"
    partial.error_message = "Provider stream ended without a terminal event"
    return partial


def _finish_reason(value: str | None, *, has_tools: bool) -> DoneReason:
    # A truncated tool call must not hide the output-limit stop reason.
    if value in {"length", "max_tokens", "MAX_TOKENS", "incomplete"}:
        return "length"
    if has_tools or value in {"tool_calls", "tool_use", "toolUse"}:
        return "toolUse"
    return "stop"
