"""Deterministic Pi-compatible model provider for tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable

from rio.ai.messages import AgentMessage
from rio.ai.provider import AssistantMessageEvent
from rio.ai.tools import AgentTool


class FakeProvider:
    """A provider that replays predefined assistant event streams."""

    def __init__(self, streams: Iterable[Iterable[AssistantMessageEvent]]) -> None:
        self._streams = [list(stream) for stream in streams]
        self.calls: list[tuple[str, str, list[AgentMessage], list[AgentTool]]] = []

    def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
    ) -> AsyncIterator[AssistantMessageEvent]:
        self.calls.append((model, system, list(messages), list(tools)))
        stream = self._streams.pop(0) if self._streams else []

        async def iterator() -> AsyncIterator[AssistantMessageEvent]:
            for event in stream:
                yield event

        return iterator()
