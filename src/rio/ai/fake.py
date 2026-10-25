"""Deterministic model provider for tests and offline examples."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from json import dumps

from rio.ai._provider_events import ProviderEvent, ProviderToolCallDeltaEvent
from rio.ai.messages import AssistantMessage, UserMessage
from rio.ai.tools import AgentTool


class FakeProvider:
    """A provider that returns predefined assistant messages in order."""

    def __init__(self, responses: Iterable[AssistantMessage]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, list[UserMessage], list[AgentTool]]] = []

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[UserMessage],
        tools: list[AgentTool],
        on_event: Callable[[ProviderEvent], None] | None = None,
    ) -> AssistantMessage:
        self.calls.append((model, system, list(messages), list(tools)))
        if not self._responses:
            return AssistantMessage(
                stop_reason="error", error_message="FakeProvider has no scripted response"
            )
        response = self._responses.pop(0)
        if on_event is not None:
            # Each scripted tool call streams as one argument fragment.
            for call in response.tool_calls:
                on_event(ProviderToolCallDeltaEvent(delta=dumps(call.arguments)))
        return response

    async def aclose(self) -> None:
        """No external resources to close."""
