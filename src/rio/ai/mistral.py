"""Mistral Conversations provider."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

from rio.ai._parsing import first_choice, loads_object
from rio.ai._provider_events import (
    ProviderErrorEvent,
    ProviderEvent,
    ProviderResponseEndEvent,
    ProviderTextDeltaEvent,
    ProviderThinkingDeltaEvent,
    ProviderToolCallEvent,
)
from rio.ai.messages import (
    AssistantMessage,
    ThinkingContent,
    Usage,
    UserMessage,
    assistant_content,
)
from rio.ai.openai_compatible import (
    OpenAICompatibleProvider,
    _parse_chunk_usage,
    _tool_call_deltas,
    _ToolCallBuilder,
)
from rio.ai.retry import RetryableFailure
from rio.ai.tools import AgentTool
from rio.ai.types import JSONValue


class MistralConversationsProvider(OpenAICompatibleProvider):
    """Provider adapter for Mistral's streaming chat API."""

    def _labels(self) -> tuple[str, str]:
        return "mistral-conversations", "mistral"

    def _stream_provider_events(
        self,
        *,
        model: str,
        system: str,
        messages: list[UserMessage],
        tools: list[AgentTool],
    ) -> AsyncIterator[ProviderEvent]:
        """Stream one Mistral response as provider-neutral events."""
        payload = _build_mistral_payload(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            reasoning_effort=self._config.reasoning_effort,
            max_tokens=self._config.max_tokens,
        )
        return self._stream(
            model=model,
            url=f"{_mistral_base_url(self._config.base_url)}/chat/completions",
            payload=payload,
            parser_factory=_MistralStreamParser,
        )


class _MistralStreamParser:
    def __init__(self) -> None:
        self.emitted_content = False
        self.fatal = False
        self._content_parts: list[str] = []
        self._thinking_parts: list[str] = []
        self._tool_call_builders: dict[int, _ToolCallBuilder] = {}
        self._finish_reason: str | None = None
        self._usage = Usage()

    def feed(self, event: str) -> tuple[list[ProviderEvent], bool]:
        if event == "[DONE]":
            return [], True
        chunk = loads_object(event)
        if chunk is None:
            return [], False
        if isinstance(chunk.get("usage"), Mapping):
            self._usage = _parse_chunk_usage(chunk["usage"])
        choice = first_choice(chunk)
        if choice is None:
            return [], False
        self._finish_reason = (
            choice.get("finish_reason") or choice.get("finishReason") or self._finish_reason
        )
        delta = choice.get("delta")
        if not isinstance(delta, Mapping):
            return [], False
        events: list[ProviderEvent] = []
        for content in _content_deltas(delta):
            self.emitted_content = True
            self._content_parts.append(content)
            events.append(ProviderTextDeltaEvent(delta=content))
        for thinking in _thinking_deltas(delta):
            self.emitted_content = True
            self._thinking_parts.append(thinking)
            events.append(ProviderThinkingDeltaEvent(delta=thinking))
        for tool_call_delta in _tool_call_deltas(delta):
            self.emitted_content = True
            index = int(tool_call_delta.get("index", 0))
            builder = self._tool_call_builders.setdefault(index, _ToolCallBuilder())
            builder.add_delta(tool_call_delta)
        return events, False

    def finalize(self) -> list[ProviderEvent]:
        if self._finish_reason is None:
            raise RetryableFailure(
                "stream ended without finish_reason",
                ProviderErrorEvent(message="Mistral stream ended without finish_reason"),
            )
        tool_calls = [
            builder.build(index) for index, builder in sorted(self._tool_call_builders.items())
        ]
        events: list[ProviderEvent] = [
            ProviderToolCallEvent(tool_call=tool_call) for tool_call in tool_calls
        ]
        content = assistant_content("".join(self._content_parts), tool_calls)
        if self._thinking_parts:
            content.insert(0, ThinkingContent(thinking="".join(self._thinking_parts)))
        events.append(
            ProviderResponseEndEvent(
                message=AssistantMessage(content=content, usage=self._usage),
                finish_reason=self._finish_reason,
            )
        )
        return events


def _build_mistral_payload(
    *,
    model: str,
    system: str,
    messages: list[UserMessage],
    tools: list[AgentTool],
    reasoning_effort: str | None,
    max_tokens: int | None,
) -> dict[str, JSONValue]:
    payload: dict[str, JSONValue] = {
        "model": model,
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [
            *_system_messages(system),
            *({"role": "user", "content": message.content} for message in messages),
        ],
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if tools:
        payload["tools"] = [_tool_to_mistral(tool) for tool in tools]
    if reasoning_effort is not None and reasoning_effort != "none":
        if _uses_reasoning_effort(model):
            payload["reasoning_effort"] = "high"
        else:
            payload["prompt_mode"] = "reasoning"
    return payload


def _system_messages(system: str) -> list[dict[str, JSONValue]]:
    return [{"role": "system", "content": system}] if system else []


def _tool_to_mistral(tool: AgentTool) -> dict[str, JSONValue]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.parameters),
            "strict": False,
        },
    }


def _mistral_base_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        return normalized
    return f"{normalized}/v1"


def _uses_reasoning_effort(model: str) -> bool:
    # Mistral Medium reasoning models ignore `prompt_mode`; only `reasoning_effort` works.
    return model.startswith("mistral-medium-") or model in {
        "mistral-small-2603",
        "mistral-small-latest",
    }


def _content_deltas(delta: Mapping[str, Any]) -> list[str]:
    content = delta.get("content")
    if isinstance(content, str) and content:
        return [content]
    if not isinstance(content, list):
        return []
    output: list[str] = []
    for item in content:
        if isinstance(item, str) and item:
            output.append(item)
        elif isinstance(item, Mapping) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str) and text:
                output.append(text)
    return output


def _thinking_deltas(delta: Mapping[str, Any]) -> list[str]:
    content = delta.get("content")
    if not isinstance(content, list):
        return []
    output: list[str] = []
    for item in content:
        if not isinstance(item, Mapping) or item.get("type") != "thinking":
            continue
        thinking = item.get("thinking")
        if isinstance(thinking, str) and thinking:
            output.append(thinking)
        elif isinstance(thinking, list):
            for part in thinking:
                if isinstance(part, Mapping):
                    text = part.get("text")
                    if isinstance(text, str) and text:
                        output.append(text)
    return output
