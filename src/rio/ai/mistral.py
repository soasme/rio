"""Mistral Conversations provider."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from json import dumps
from typing import Any

import httpx

from rio.ai._parsing import first_choice, loads_object, parse_sse_line, tool_arguments
from rio.ai._provider_events import (
    ProviderEvent,
    ProviderResponseEndEvent,
    ProviderTextDeltaEvent,
    ProviderThinkingDeltaEvent,
    ProviderToolCallEvent,
)
from rio.ai.content import (
    NON_VISION_TOOL_IMAGE_PLACEHOLDER,
    NON_VISION_USER_IMAGE_PLACEHOLDER,
    text_and_images,
)
from rio.ai.env import OpenAICompatibleConfig
from rio.ai.http import create_async_client, request_with_retries
from rio.ai.messages import (
    AgentMessage,
    AssistantMessage,
    ImageContent,
    ThinkingContent,
    ToolResultMessage,
    UserMessage,
    assistant_content,
)
from rio.ai.provider import AssistantMessageEvent, collect_response
from rio.ai.retry import RetryAttempt
from rio.ai.tool_call_ids import portable_tool_call_id
from rio.ai.tools import AgentTool, ToolCall
from rio.ai.types import JSONValue


class MistralConversationsProvider:
    """Provider adapter for Mistral's streaming chat API."""

    def __init__(
        self,
        config: OpenAICompatibleConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._owns_client = client is None

    async def aclose(self) -> None:
        """Close the underlying HTTP client if this provider created it."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
    ) -> AsyncIterator[AssistantMessageEvent]:
        """Collect one response as a final assistant message or error."""
        raw = self._stream_provider_events(
            model=model, system=system, messages=messages, tools=tools
        )
        return collect_response(raw, api="mistral-conversations", provider="mistral", model=model)

    def _stream_provider_events(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
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
            supports_images=self._config.supports_images,
        )
        return self._stream(
            model=model,
            url=f"{_mistral_base_url(self._config.base_url)}/chat/completions",
            payload=payload,
        )

    def _stream(
        self,
        *,
        model: str,
        url: str,
        payload: Mapping[str, JSONValue],
    ) -> AsyncIterator[ProviderEvent]:
        async def iterator() -> AsyncIterator[ProviderEvent]:
            client = self._get_client()
            headers = {
                **dict(self._config.headers or {}),
                "Authorization": f"Bearer {self._config.api_key}",
            }

            async def parse(
                response: httpx.Response, attempt: RetryAttempt
            ) -> AsyncIterator[ProviderEvent]:
                parser = _MistralStreamParser()
                async for line in response.aiter_lines():
                    event = parse_sse_line(line)
                    if event is None:
                        continue
                    events, stop = parser.feed(event)
                    for parser_event in events:
                        yield parser_event
                    attempt.emitted_content = parser.emitted_content
                    if stop:
                        break
                for parser_event in parser.finalize():
                    yield parser_event

            async for event in request_with_retries(
                client,
                url=url,
                payload=payload,
                headers=headers,
                model=model,
                config=self._config,
                parse=parse,
            ):
                yield event

        return iterator()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = create_async_client(timeout=self._config.timeout_seconds)
        return self._client


class _MistralStreamParser:
    def __init__(self) -> None:
        self.emitted_content = False
        self._content_parts: list[str] = []
        self._thinking_parts: list[str] = []
        self._tool_call_builders: dict[int, _ToolCallBuilder] = {}
        self._finish_reason: str | None = None

    def feed(self, event: str) -> tuple[list[ProviderEvent], bool]:
        if event == "[DONE]":
            return [], True
        chunk = loads_object(event)
        if chunk is None:
            return [], False
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
                message=AssistantMessage(content=content),
                finish_reason=self._finish_reason or ("tool_calls" if tool_calls else "stop"),
            )
        )
        return events


class _ToolCallBuilder:
    def __init__(self) -> None:
        self.id = ""
        self.name = ""
        self.arguments_parts: list[str] = []

    def add_delta(self, delta: Mapping[str, Any]) -> None:
        call_id = delta.get("id")
        if isinstance(call_id, str) and call_id != "null":
            self.id = call_id
        function = delta.get("function")
        if not isinstance(function, Mapping):
            return
        name = function.get("name")
        if isinstance(name, str):
            self.name = name
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            self.arguments_parts.append(arguments)
        elif isinstance(arguments, Mapping):
            self.arguments_parts.append(dumps(arguments))

    def build(self, index: int) -> ToolCall:
        return ToolCall(
            id=self.id or f"tool-call-{index}",
            name=self.name,
            arguments=tool_arguments("".join(self.arguments_parts)),
        )


def _build_mistral_payload(
    *,
    model: str,
    system: str,
    messages: list[AgentMessage],
    tools: list[AgentTool],
    reasoning_effort: str | None,
    max_tokens: int | None,
    supports_images: bool = False,
) -> dict[str, JSONValue]:
    payload: dict[str, JSONValue] = {
        "model": model,
        "stream": True,
        "messages": [
            *_system_messages(system),
            *_messages_to_mistral(messages, supports_images=supports_images),
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


def _messages_to_mistral(
    messages: list[AgentMessage], *, supports_images: bool
) -> list[dict[str, JSONValue]]:
    converted: list[dict[str, JSONValue]] = []
    pending_tool_images: list[ImageContent] = []
    for message in messages:
        if pending_tool_images and not isinstance(message, ToolResultMessage):
            converted.append(_mistral_tool_image_message(pending_tool_images))
            pending_tool_images = []
        if isinstance(message, UserMessage):
            text, images = text_and_images(
                message.content,
                supports_images=supports_images,
                image_placeholder=NON_VISION_USER_IMAGE_PLACEHOLDER,
            )
            if images:
                content: list[JSONValue] = []
                if text:
                    content.append({"type": "text", "text": text})
                content.extend(
                    {
                        "type": "image_url",
                        "image_url": f"data:{image.mime_type};base64,{image.data}",
                    }
                    for image in images
                )
                converted.append({"role": "user", "content": content})
            else:
                converted.append({"role": "user", "content": text})
            continue
        if isinstance(message, ToolResultMessage):
            text, images = text_and_images(
                message.content,
                supports_images=supports_images,
                image_placeholder=NON_VISION_TOOL_IMAGE_PLACEHOLDER,
            )
            converted.append(
                {
                    "role": "tool",
                    "tool_call_id": portable_tool_call_id(message.tool_call_id),
                    "name": message.tool_name,
                    "content": text or ("(see attached image)" if images else "(no tool output)"),
                }
            )
            pending_tool_images.extend(images)
            continue
        converted.append(_message_to_mistral(message))
    if pending_tool_images:
        converted.append(_mistral_tool_image_message(pending_tool_images))
    return converted


def _mistral_tool_image_message(images: list[ImageContent]) -> dict[str, JSONValue]:
    content: list[JSONValue] = [{"type": "text", "text": "Attached image(s) from tool result:"}]
    content.extend(
        {
            "type": "image_url",
            "image_url": f"data:{image.mime_type};base64,{image.data}",
        }
        for image in images
    )
    return {"role": "user", "content": content}


def _message_to_mistral(message: AgentMessage) -> dict[str, JSONValue]:
    if isinstance(message, UserMessage):
        return {"role": "user", "content": message.text}
    if isinstance(message, AssistantMessage):
        item: dict[str, JSONValue] = {"role": "assistant", "content": message.text}
        if message.thinking_text:
            item["reasoning_content"] = message.thinking_text
        if message.tool_calls:
            item["tool_calls"] = [
                _tool_call_to_mistral(tool_call) for tool_call in message.tool_calls
            ]
        return item
    if isinstance(message, ToolResultMessage):
        return {
            "role": "tool",
            "tool_call_id": portable_tool_call_id(message.tool_call_id),
            "name": message.tool_name,
            "content": message.text,
        }
    raise TypeError(f"Unsupported message: {type(message).__name__}")


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


def _tool_call_to_mistral(tool_call: ToolCall) -> dict[str, JSONValue]:
    return {
        "id": portable_tool_call_id(tool_call.id),
        "type": "function",
        "function": {"name": tool_call.name, "arguments": dumps(tool_call.arguments)},
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


def _tool_call_deltas(delta: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    tool_calls = delta.get("tool_calls") or delta.get("toolCalls")
    if not isinstance(tool_calls, list):
        return []
    return [tool_call for tool_call in tool_calls if isinstance(tool_call, Mapping)]
