"""Anthropic Messages API provider."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping
from typing import cast

import httpx

from rio.ai._parsing import int_or_none, loads_object, parse_sse_line, tool_arguments
from rio.ai._provider_events import (
    ProviderErrorEvent,
    ProviderEvent,
    ProviderResponseEndEvent,
    ProviderTextDeltaEvent,
    ProviderThinkingDeltaEvent,
    ProviderToolCallEvent,
)
from rio.ai.env import (
    CACHE_RETENTION_LONG,
    CACHE_RETENTION_NONE,
    CACHE_RETENTION_SHORT,
    AnthropicConfig,
    CacheRetention,
)
from rio.ai.http import HTTPProvider, request_with_retries
from rio.ai.messages import (
    AssistantMessage,
    ThinkingContent,
    Usage,
    UserMessage,
    assistant_content,
)
from rio.ai.retry import (
    RetryableFailure,
    RetryAttempt,
)
from rio.ai.tools import AgentTool, ToolCall
from rio.ai.types import JSONValue

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 4096
CACHE_TTL_LONG = "1h"


class AnthropicProvider(HTTPProvider[AnthropicConfig]):
    """Provider adapter for Anthropic's streaming Messages API."""

    def _labels(self) -> tuple[str, str]:
        return "anthropic-messages", "anthropic"

    def _stream_provider_events(
        self,
        *,
        model: str,
        system: str,
        messages: list[UserMessage],
        tools: list[AgentTool],
    ) -> AsyncGenerator[ProviderEvent]:
        """Stream one Anthropic response as provider-neutral events."""

        async def iterator() -> AsyncGenerator[ProviderEvent]:
            client = self._get_client()
            api_key = self._config.api_key
            base_url = self._config.base_url
            auth_headers: dict[str, str] = {}
            if self._config.credential_resolver is not None:
                auth = await self._config.credential_resolver()
                api_key = auth.api_key
                if auth.base_url is not None:
                    base_url = auth.base_url.rstrip("/")
                    if not base_url.endswith("/v1"):
                        base_url = f"{base_url}/v1"
                auth_headers.update(auth.headers or {})
            payload = _build_messages_payload(
                model=model,
                system=system,
                oauth_system_prompt=self._config.oauth_system_prompt,
                messages=messages,
                tools=tools,
                max_tokens=self._config.max_tokens,
                thinking_budget_tokens=self._config.thinking_budget_tokens,
                thinking_effort=self._config.thinking_effort,
                thinking_mode=self._config.thinking_mode,
                cache_retention=self._config.cache_retention,
                cache_control_on_tools=self._config.cache_control_on_tools,
            )
            headers = {
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
                **(dict(self._config.headers or {})),
                **auth_headers,
            }
            if self._config.bearer_auth:
                headers.setdefault("Authorization", f"Bearer {api_key}")
            else:
                headers["x-api-key"] = api_key
            url = f"{base_url.rstrip('/')}/messages"

            async def parse(
                response: httpx.Response, attempt: RetryAttempt
            ) -> AsyncGenerator[ProviderEvent]:
                content_parts: list[str] = []
                thinking_parts: list[str] = []
                tool_builders: dict[int, _AnthropicToolBuilder] = {}
                finish_reason: str | None = None
                usage: Usage | None = None

                async for line in response.aiter_lines():
                    event = parse_sse_line(line)
                    if event is None:
                        continue
                    chunk = loads_object(event)
                    if chunk is None:
                        yield ProviderErrorEvent(message="Provider returned invalid JSON chunk")
                        return

                    event_type = chunk.get("type")
                    if event_type == "message_start":
                        message = chunk.get("message")
                        if isinstance(message, Mapping):
                            usage = _usage_from_message_start(message.get("usage"))
                    elif event_type == "content_block_start":
                        block = chunk.get("content_block")
                        if isinstance(block, Mapping) and block.get("type") == "tool_use":
                            index = int(chunk.get("index", 0))
                            builder = tool_builders.setdefault(index, _AnthropicToolBuilder())
                            builder.id = _string_or_empty(block.get("id"))
                            builder.name = _string_or_empty(block.get("name"))
                            attempt.emitted_content = True
                    elif event_type == "content_block_delta":
                        delta = chunk.get("delta")
                        if not isinstance(delta, Mapping):
                            continue
                        delta_type = delta.get("type")
                        if delta_type == "text_delta":
                            text = _string_or_empty(delta.get("text"))
                            if text:
                                attempt.emitted_content = True
                                content_parts.append(text)
                                yield ProviderTextDeltaEvent(delta=text)
                        elif delta_type == "thinking_delta":
                            thinking = _string_or_empty(delta.get("thinking"))
                            if thinking:
                                attempt.emitted_content = True
                                thinking_parts.append(thinking)
                                yield ProviderThinkingDeltaEvent(delta=thinking)
                        elif delta_type == "input_json_delta":
                            index = int(chunk.get("index", 0))
                            builder = tool_builders.setdefault(index, _AnthropicToolBuilder())
                            builder.arguments_parts.append(
                                _string_or_empty(delta.get("partial_json"))
                            )
                            attempt.emitted_content = True
                    elif event_type == "message_delta":
                        delta = chunk.get("delta")
                        if isinstance(delta, Mapping):
                            finish_reason = (
                                _string_or_empty(delta.get("stop_reason")) or finish_reason
                            )
                        usage = _apply_message_delta_usage(usage, chunk.get("usage"))
                    elif event_type == "error":
                        error_type, message = _anthropic_stream_error_details(chunk)
                        stream_error = ProviderErrorEvent(
                            message=message,
                            data={"event": chunk, "attempts": attempt.number + 1},
                        )
                        if _retryable_anthropic_stream_error(error_type):
                            raise RetryableFailure(
                                f"stream error ({error_type or 'unknown'})",
                                stream_error,
                                data={"event": chunk},
                            )
                        yield stream_error
                        return

                if finish_reason is None:
                    raise RetryableFailure(
                        "stream ended without stop_reason",
                        ProviderErrorEvent(message="Anthropic stream ended without stop_reason"),
                    )
                tool_calls = [
                    builder.build(index) for index, builder in sorted(tool_builders.items())
                ]
                for tool_call in tool_calls:
                    yield ProviderToolCallEvent(tool_call=tool_call)

                content = assistant_content("".join(content_parts), tool_calls)
                if thinking_parts:
                    content.insert(0, ThinkingContent(thinking="".join(thinking_parts)))
                yield ProviderResponseEndEvent(
                    message=AssistantMessage(content=content, usage=usage or Usage()),
                    finish_reason=finish_reason,
                )

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


_TRANSIENT_ANTHROPIC_STREAM_ERROR_TYPES = frozenset(
    {
        "api_error",
        "overloaded_error",
        "rate_limit_error",
    }
)


def _anthropic_stream_error_details(event: Mapping[str, JSONValue]) -> tuple[str, str]:
    """Return the provider classification and message from an Anthropic SSE error."""
    error = event.get("error")
    if not isinstance(error, Mapping):
        return "", "Provider returned an error"
    error_type = _string_or_empty(error.get("type"))
    message = _string_or_empty(error.get("message")) or "Provider returned an error"
    return error_type, message


def _retryable_anthropic_stream_error(error_type: str) -> bool:
    """Return whether an Anthropic SSE error is transient and safe to retry."""
    return error_type.lower() in _TRANSIENT_ANTHROPIC_STREAM_ERROR_TYPES


class _AnthropicToolBuilder:
    def __init__(self) -> None:
        self.id = ""
        self.name = ""
        self.arguments_parts: list[str] = []

    def build(self, index: int) -> ToolCall:
        return ToolCall(
            id=self.id or f"tool-call-{index}",
            name=self.name,
            arguments=tool_arguments("".join(self.arguments_parts)),
        )


def _build_messages_payload(
    *,
    model: str,
    system: str,
    messages: list[UserMessage],
    oauth_system_prompt: str | None = None,
    tools: list[AgentTool],
    max_tokens: int | None = None,
    thinking_budget_tokens: int | None = None,
    thinking_effort: str | None = None,
    thinking_mode: str = "budget",
    cache_retention: CacheRetention = CACHE_RETENTION_SHORT,
    cache_control_on_tools: bool = True,
) -> dict[str, JSONValue]:
    resolved_max_tokens = max_tokens or DEFAULT_MAX_TOKENS
    if thinking_budget_tokens is not None:
        resolved_max_tokens = max(resolved_max_tokens, thinking_budget_tokens + 1024)
    cache_control = _cache_control(cache_retention)
    payload_messages: list[dict[str, JSONValue]] = [
        {"role": "user", "content": message.content} for message in messages
    ]
    # One breakpoint on the request tail; the system prompt and tools carry the others.
    if cache_control is not None and payload_messages and payload_messages[-1]["content"]:
        tail = payload_messages[-1]
        tail["content"] = [
            {"type": "text", "text": tail["content"], "cache_control": dict(cache_control)}
        ]
    payload: dict[str, JSONValue] = {
        "model": model,
        "max_tokens": resolved_max_tokens,
        "stream": True,
        "system": _anthropic_system(system, oauth_system_prompt, cache_control),
        "messages": cast("JSONValue", payload_messages),
    }
    if thinking_mode == "disabled":
        payload["thinking"] = {"type": "disabled"}
    elif thinking_mode == "adaptive" and thinking_effort is not None:
        payload["thinking"] = {"type": "adaptive", "display": "summarized"}
        payload["output_config"] = {"effort": thinking_effort}
    elif thinking_budget_tokens is not None:
        payload["thinking"] = {
            "type": "enabled",
            "budget_tokens": thinking_budget_tokens,
        }
    if tools:
        # Some Anthropic-protocol gateways accept cache_control everywhere except
        # inside tool objects, so the tools breakpoint is separately suppressible.
        tools_cache_control = cache_control if cache_control_on_tools else None
        last_index = len(tools) - 1
        payload["tools"] = [
            _anthropic_tool(
                tool,
                cache_control=tools_cache_control if index == last_index else None,
            )
            for index, tool in enumerate(tools)
        ]
    return payload


def _cache_control(cache_retention: CacheRetention) -> dict[str, JSONValue] | None:
    """Return the cache_control marker for a retention preference, if enabled.

    Attach sites copy the result, so no two breakpoints share one dict.
    """
    if cache_retention == CACHE_RETENTION_NONE:
        return None
    if cache_retention == CACHE_RETENTION_LONG:
        return {"type": "ephemeral", "ttl": CACHE_TTL_LONG}
    return {"type": "ephemeral"}


def _anthropic_system(
    system: str,
    oauth_system_prompt: str | None,
    cache_control: dict[str, JSONValue] | None,
) -> JSONValue:
    """Build the system field, marking its tail as a cache breakpoint when enabled.

    Only the final block is marked. A breakpoint on the OAuth identity block would
    cache a prefix already covered by the block after it, wasting one of the four
    breakpoints Anthropic allows.
    """
    if cache_control is None:
        if oauth_system_prompt:
            return [
                {"type": "text", "text": oauth_system_prompt},
                {"type": "text", "text": system},
            ]
        return system
    blocks: list[dict[str, JSONValue]] = []
    if oauth_system_prompt:
        blocks.append({"type": "text", "text": oauth_system_prompt})
    if system:
        blocks.append({"type": "text", "text": system})
    if not blocks:
        # An empty text block carrying cache_control is rejected outright.
        return system
    blocks[-1]["cache_control"] = dict(cache_control)
    return cast("JSONValue", blocks)


def _anthropic_tool(
    tool: AgentTool,
    *,
    cache_control: dict[str, JSONValue] | None = None,
) -> dict[str, JSONValue]:
    payload: dict[str, JSONValue] = {
        "name": tool.name,
        "description": tool.description,
        "input_schema": dict(tool.parameters),
    }
    if cache_control is not None:
        payload["cache_control"] = dict(cache_control)
    return payload


def _string_or_empty(value: object) -> str:
    return value if isinstance(value, str) else ""


def _usage_from_message_start(raw: object) -> Usage:
    """Build a Usage from the ``message_start`` event's ``message.usage``.

    Ports Pi's anthropic-messages.ts message_start handling. Cost is left unset
    (None) because Rio has no per-model pricing table.
    """
    data = raw if isinstance(raw, Mapping) else {}
    cache_creation = data.get("cache_creation")
    cache_write_1h = (
        int_or_none(cache_creation.get("ephemeral_1h_input_tokens"))
        if isinstance(cache_creation, Mapping)
        else None
    )
    usage = Usage(
        input=int_or_none(data.get("input_tokens")) or 0,
        output=int_or_none(data.get("output_tokens")) or 0,
        cache_read=int_or_none(data.get("cache_read_input_tokens")) or 0,
        cache_write=int_or_none(data.get("cache_creation_input_tokens")) or 0,
        cache_write_1h=cache_write_1h,
    )
    usage.total_tokens = usage.input + usage.output + usage.cache_read + usage.cache_write
    return usage


def _apply_message_delta_usage(usage: Usage | None, raw: object) -> Usage | None:
    """Apply the ``message_delta`` event's ``usage`` onto the running Usage.

    Ports Pi's anthropic-messages.ts message_delta handling: only overwrite
    fields the provider reports (non-null), then recompute the token total.
    """
    if not isinstance(raw, Mapping):
        return usage
    usage = usage or Usage()
    if (value := int_or_none(raw.get("input_tokens"))) is not None:
        usage.input = value
    if (value := int_or_none(raw.get("output_tokens"))) is not None:
        usage.output = value
    if (value := int_or_none(raw.get("cache_read_input_tokens"))) is not None:
        usage.cache_read = value
    if (value := int_or_none(raw.get("cache_creation_input_tokens"))) is not None:
        usage.cache_write = value
    details = raw.get("output_tokens_details")
    if isinstance(details, Mapping):
        thinking = int_or_none(details.get("thinking_tokens"))
        if thinking is not None:
            usage.reasoning = thinking
    usage.total_tokens = usage.input + usage.output + usage.cache_read + usage.cache_write
    return usage
