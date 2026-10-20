"""OpenAI-compatible chat completions provider.

Most OpenAI-compatible models are served over `/chat/completions`. Newer
reasoning models (e.g. ``gpt-5.5``/``gpt-5.4`` and the ``*-codex`` family)
reject the combination of function tools and ``reasoning_effort`` on that
endpoint and require ``/v1/responses`` instead. This adapter routes those
models to the Responses API at request time while leaving every other model on
the original chat-completions path unchanged.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Mapping
from json import dumps
from typing import Any, Protocol

import httpx

from rio.ai._parsing import (
    first_choice,
    int_or_none,
    int_or_zero,
    loads_object,
    parse_sse_line,
    tool_arguments,
)
from rio.ai._provider_events import (
    ProviderErrorEvent,
    ProviderEvent,
    ProviderResponseEndEvent,
    ProviderTextDeltaEvent,
    ProviderThinkingDeltaEvent,
    ProviderToolCallEvent,
)
from rio.ai.env import OpenAICompatibleConfig
from rio.ai.http import HTTPProvider, request_with_retries
from rio.ai.messages import (
    AssistantMessage,
    ThinkingContent,
    Usage,
    UserMessage,
    assistant_content,
)
from rio.ai.retry import RetryableFailure, RetryAttempt
from rio.ai.tools import AgentTool, ToolCall
from rio.ai.types import JSONValue

# Models that reject function tools + reasoning_effort on /chat/completions and
# must use the /v1/responses endpoint instead.
_RESPONSES_ONLY_PREFIXES: tuple[str, ...] = ("gpt-5.5", "gpt-5.4")


def _use_responses_api(model: str) -> bool:
    """Return whether ``model`` must be served over the Responses API."""
    normalized = model.strip().lower()
    if "codex" in normalized:
        return True
    return any(normalized.startswith(prefix) for prefix in _RESPONSES_ONLY_PREFIXES)


class OpenAICompatibleProvider(HTTPProvider[OpenAICompatibleConfig]):
    """Provider adapter for OpenAI-compatible `/chat/completions` APIs.

    Models that require it are transparently served over `/v1/responses`.
    """

    def _labels(self) -> tuple[str, str]:
        return self._config.api, self._config.provider_name

    def _stream_provider_events(
        self,
        *,
        model: str,
        system: str,
        messages: list[UserMessage],
        tools: list[AgentTool],
    ) -> AsyncGenerator[ProviderEvent]:
        """Stream one model response as provider-neutral events."""
        if self._config.api == "openai-responses" or _use_responses_api(model):
            return self._stream_responses(
                model=model,
                system=system,
                messages=messages,
                tools=tools,
            )
        return self._stream_chat_completions(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
        )

    def _stream_chat_completions(
        self,
        *,
        model: str,
        system: str,
        messages: list[UserMessage],
        tools: list[AgentTool],
    ) -> AsyncGenerator[ProviderEvent]:
        """Stream one chat completion response as provider-neutral events."""
        payload = _build_chat_payload(
            model=self._config.model_aliases.get(model, model),
            system=system,
            messages=messages,
            tools=tools,
            reasoning_effort=self._config.reasoning_effort,
            reasoning_effort_parameter=self._config.reasoning_effort_parameter,
            thinking_format=self._config.thinking_format,
            compat=self._config.compat,
            max_tokens=self._config.max_tokens,
            include_reasoning_effort_none=self._config.include_reasoning_effort_none,
        )
        return self._stream(
            model=model,
            url=f"{self._config.base_url.rstrip('/')}/chat/completions",
            payload=payload,
            parser_factory=_ChatStreamParser,
        )

    def _stream_responses(
        self,
        *,
        model: str,
        system: str,
        messages: list[UserMessage],
        tools: list[AgentTool],
    ) -> AsyncGenerator[ProviderEvent]:
        """Stream one `/v1/responses` response as provider-neutral events."""
        payload = _build_responses_payload(
            model=self._config.model_aliases.get(model, model),
            system=system,
            messages=messages,
            tools=tools,
            reasoning_effort=self._config.reasoning_effort,
            max_tokens=self._config.max_tokens,
            compat=self._config.compat,
        )
        return self._stream(
            model=model,
            url=f"{self._config.base_url.rstrip('/')}/responses",
            payload=payload,
            parser_factory=_ResponsesStreamParser,
        )

    def _stream(
        self,
        *,
        model: str,
        url: str,
        payload: Mapping[str, JSONValue],
        parser_factory: Callable[[], _StreamParser],
    ) -> AsyncGenerator[ProviderEvent]:
        """Run the shared streaming POST + retry envelope for a given endpoint.

        The per-endpoint differences (SSE chunk handling and final-message
        assembly) live in the ``_StreamParser`` produced by ``parser_factory``;
        everything else — HTTP, status/network retries, the
        opening ``response_start`` event — is identical across endpoints.
        """

        async def iterator() -> AsyncGenerator[ProviderEvent]:
            client = self._get_client()
            api_key = self._config.api_key
            request_url = url
            headers = dict(self._config.headers or {})
            if self._config.credential_resolver is not None:
                auth = await self._config.credential_resolver()
                api_key = auth.api_key
                headers.update(auth.headers or {})
                if auth.base_url is not None:
                    endpoint = (
                        "/responses"
                        if url.rstrip("/").endswith("/responses")
                        else "/chat/completions"
                    )
                    request_url = f"{auth.base_url.rstrip('/')}{endpoint}"
            has_authorization = any(key.casefold() == "authorization" for key in headers)
            if not has_authorization and api_key is not None:
                headers["Authorization"] = f"Bearer {api_key}"

            async for event in request_with_retries(
                client,
                url=request_url,
                payload=payload,
                headers=headers,
                model=model,
                config=self._config,
                parse=lambda response, attempt: _parse_sse(response, attempt, parser_factory()),
                response_provider_header=self._config.response_provider_header,
            ):
                yield event

        return iterator()


class _StreamParser(Protocol):
    """Per-endpoint SSE handler driven by the shared streaming envelope."""

    # True once any model output (text/thinking/tool args) has been emitted;
    # the envelope uses it to decide whether a mid-stream drop is retryable.
    emitted_content: bool
    # True when the parser already emitted a terminal error event and the
    # envelope must not call finalize().
    fatal: bool

    def feed(self, event: str) -> tuple[list[ProviderEvent], bool]:
        """Consume one SSE ``data:`` payload, returning (events, should_stop)."""
        ...

    def finalize(self) -> list[ProviderEvent]:
        """Return the trailing tool-call and response-end events."""
        ...


async def _parse_sse(
    response: httpx.Response, attempt: RetryAttempt, parser: _StreamParser
) -> AsyncGenerator[ProviderEvent]:
    """Drive ``parser`` over a chat-style SSE body, recording emitted content."""
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
    if parser.fatal:
        return
    for parser_event in parser.finalize():
        yield parser_event


class _ChatStreamParser:
    """Parser for OpenAI `/chat/completions` SSE chunks."""

    def __init__(self) -> None:
        self.emitted_content = False
        self.fatal = False
        self._content_parts: list[str] = []
        self._thinking_parts: list[str] = []
        self._tool_call_builders: dict[int, _ToolCallBuilder] = {}
        self._finish_reason: str | None = None
        self._usage: Usage | None = None

    def feed(self, event: str) -> tuple[list[ProviderEvent], bool]:
        if event == "[DONE]":
            return [], True

        chunk = loads_object(event)
        if chunk is None:
            self.fatal = True
            return [ProviderErrorEvent(message="Provider returned invalid JSON chunk")], True

        # The final usage chunk (from stream_options) carries usage at the top
        # level and often has empty choices.
        chunk_usage = chunk.get("usage")
        if isinstance(chunk_usage, Mapping):
            self._usage = _parse_chunk_usage(chunk_usage)

        choice = first_choice(chunk)
        if choice is None:
            return [], False

        # Fallback: some providers (e.g. Moonshot) attach usage to the choice
        # instead of the chunk. Matches Pi's per-chunk `!chunk.usage` guard: the
        # fallback applies whenever this chunk lacks top-level usage.
        choice_usage = choice.get("usage")
        if not isinstance(chunk_usage, Mapping) and isinstance(choice_usage, Mapping):
            self._usage = _parse_chunk_usage(choice_usage)

        self._finish_reason = choice.get("finish_reason") or self._finish_reason
        delta = choice.get("delta")
        if not isinstance(delta, Mapping):
            return [], False

        events: list[ProviderEvent] = []
        content = delta.get("content")
        if isinstance(content, str) and content:
            self.emitted_content = True
            self._content_parts.append(content)
            events.append(ProviderTextDeltaEvent(delta=content))

        thinking = _thinking_delta(delta)
        if thinking is not None:
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
                ProviderErrorEvent(message="Provider stream ended without finish_reason"),
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
                message=AssistantMessage(
                    content=content,
                    usage=self._usage or Usage(),
                ),
                finish_reason=self._finish_reason,
            )
        )
        return events


class _ResponsesStreamParser:
    """Parser for OpenAI `/v1/responses` SSE events."""

    def __init__(self, *, codex: bool = False) -> None:
        self._codex = codex
        self.emitted_content = False
        self.fatal = False
        self._content_parts: list[str] = []
        self._thinking_parts: list[str] = []
        self._tool_call_builders: dict[str, _ResponsesToolCallBuilder] = {}
        self._status: str | None = None
        self._usage: Usage | None = None
        self._saw_terminal_event = False

    def feed(self, event: str) -> tuple[list[ProviderEvent], bool]:
        # The Responses API has no [DONE] sentinel; it ends with a terminal
        # event (completed/incomplete/failed) handled below.
        if event == "[DONE]":
            return [], False

        chunk = loads_object(event)
        if chunk is None:
            return [], False

        chunk_type = chunk.get("type")
        if not isinstance(chunk_type, str):
            return [], False

        if chunk_type in ("response.output_text.delta", "response.refusal.delta"):
            delta = chunk.get("delta")
            if isinstance(delta, str) and delta:
                self.emitted_content = True
                self._content_parts.append(delta)
                return [ProviderTextDeltaEvent(delta=delta)], False

        elif chunk_type in (
            "response.reasoning.delta",
            "response.reasoning_summary_text.delta",
            "response.reasoning_text.delta",
        ):
            delta = chunk.get("delta")
            if isinstance(delta, str) and delta:
                self.emitted_content = True
                self._thinking_parts.append(delta)
                return [ProviderThinkingDeltaEvent(delta=delta)], False

        elif chunk_type == "response.reasoning_summary_part.done":
            if self._thinking_parts:
                separator = "\n\n"
                self._thinking_parts.append(separator)
                return [ProviderThinkingDeltaEvent(delta=separator)], False

        elif chunk_type == "response.output_item.added":
            self._tool_builder(chunk)

        elif chunk_type == "response.function_call_arguments.delta":
            builder = self._tool_builder(chunk)
            if builder is not None:
                builder.add_arguments_delta(chunk.get("delta"))
                self.emitted_content = True

        elif chunk_type == "response.function_call_arguments.done":
            builder = self._tool_builder(chunk)
            if builder is not None:
                builder.set_final(
                    name=_str_or_none(chunk.get("name")), arguments=chunk.get("arguments")
                )
                builder.done = not self._codex

        elif chunk_type in ("response.output_item.done", "response.output_item.completed"):
            item = chunk.get("item")
            builder = self._tool_builder(chunk)
            if builder is not None:
                builder.done = True
                if self._codex:
                    return [ProviderToolCallEvent(tool_call=builder.build(0))], False
            if (
                isinstance(item, Mapping)
                and item.get("type") == "message"
                and not self._content_parts
            ):
                content = item.get("content")
                text = "".join(
                    value
                    for part in (content if isinstance(content, list) else [])
                    if isinstance(part, Mapping)
                    for value in [
                        part.get("text")
                        if part.get("type") == "output_text"
                        else part.get("refusal")
                    ]
                    if isinstance(value, str)
                )
                if text:
                    self.emitted_content = True
                    self._content_parts.append(text)
                    return [ProviderTextDeltaEvent(delta=text)], False

        elif chunk_type in ("response.done", "response.completed", "response.incomplete"):
            self._saw_terminal_event = True
            self._status = _responses_finish_reason(chunk)
            if chunk_type == "response.incomplete":
                self._status = "incomplete"
            self._usage = _usage_from_responses_event(chunk) or self._usage
            return [], True

        elif chunk_type == "response.failed":
            self.fatal = True
            return [_responses_failure_event(chunk)], True

        elif chunk_type == "error":
            self.fatal = True
            return [
                ProviderErrorEvent(message=_responses_error_message(chunk), data={"event": chunk})
            ], True

        return [], False

    def _tool_builder(self, chunk: Mapping[str, Any]) -> _ResponsesToolCallBuilder | None:
        item = chunk.get("item")
        if (item is not None or chunk["type"].startswith("response.output_item.")) and (
            not isinstance(item, Mapping) or item.get("type") != "function_call"
        ):
            return None
        fields = item if isinstance(item, Mapping) else chunk
        item_id = fields.get("id") if item is not None else chunk.get("item_id")
        key = _responses_item_key(chunk.get("output_index"), item_id)
        if self._codex and key not in self._tool_call_builders:
            for known_key, builder in self._tool_call_builders.items():
                if (item_id and item_id == builder.item_id) or (
                    fields.get("call_id") and fields["call_id"] == builder.call_id
                ):
                    key = known_key
                    break
            else:
                active = [
                    (key, builder)
                    for key, builder in self._tool_call_builders.items()
                    if not builder.done
                ]
                if item is None:
                    # Codex argument fragments must belong to an announced call.
                    return active[0][1] if len(active) == 1 else None
                key = key or f"call:{fields.get('call_id') or len(self._tool_call_builders)}"
        if key is None:
            return None
        builder = self._tool_call_builders.setdefault(key, _ResponsesToolCallBuilder())
        builder.set_final(
            call_id=_str_or_none(fields.get("call_id")),
            item_id=_str_or_none(item_id),
            name=_str_or_none(fields.get("name")),
            arguments=(fields.get("arguments") or None)
            if chunk.get("type") == "response.output_item.added"
            else fields.get("arguments"),
            output_index=int_or_none(chunk.get("output_index")),
        )
        return builder

    def finalize(self) -> list[ProviderEvent]:
        if not self._saw_terminal_event:
            return [
                ProviderErrorEvent(
                    message="OpenAI Responses stream ended before a terminal response event"
                )
            ]
        builders = _ordered_builders(self._tool_call_builders)
        # The agent runs every tool call in the final message. Refuse calls that never
        # got a done event: their arguments may be cut off or mixed up, e.g. when a
        # non-compliant server omits output_index.
        for builder in builders:
            if not builder.done:
                return [
                    ProviderErrorEvent(
                        message=(
                            "OpenAI Responses stream completed with an unfinished tool call: "
                            f"{builder.name} ({builder.call_id})"
                        )
                    )
                ]
        tool_calls = [builder.build(index) for index, builder in enumerate(builders)]
        events: list[ProviderEvent] = [
            ProviderToolCallEvent(tool_call=tool_call)
            for tool_call in tool_calls
            if not self._codex
        ]
        content = assistant_content("".join(self._content_parts), tool_calls)
        if self._thinking_parts:
            content.insert(0, ThinkingContent(thinking="".join(self._thinking_parts)))
        events.append(
            ProviderResponseEndEvent(
                message=AssistantMessage(
                    content=content,
                    usage=self._usage or Usage(),
                ),
                finish_reason=self._status,
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


class _ResponsesToolCallBuilder:
    """Accumulates a streamed Responses-API ``function_call`` output item."""

    def __init__(
        self,
        *,
        call_id: str = "",
        name: str = "",
        output_index: int = 0,
    ) -> None:
        self.item_id: str | None = None
        self.call_id = call_id
        self.name = name
        self.output_index = output_index
        self.arguments_parts: list[str] = []
        self.arguments_final: str | None = None
        self.done = False

    def add_arguments_delta(self, delta: object) -> None:
        if isinstance(delta, str):
            self.arguments_parts.append(delta)

    def set_final(
        self,
        *,
        call_id: str | None = None,
        item_id: str | None = None,
        name: str | None = None,
        arguments: object = None,
        output_index: int | None = None,
    ) -> None:
        if item_id:
            self.item_id = item_id
        if call_id:
            self.call_id = call_id
        if name:
            self.name = name
        if isinstance(arguments, str):
            self.arguments_final = arguments
        if output_index is not None:
            self.output_index = output_index

    def build(self, index: int) -> ToolCall:
        arguments_text = (
            self.arguments_final
            if self.arguments_final is not None
            else "".join(self.arguments_parts)
        )
        return ToolCall(
            id=self.call_id or f"tool-call-{index}",
            name=self.name,
            arguments=tool_arguments(arguments_text),
        )


def _build_chat_payload(
    *,
    model: str,
    system: str,
    messages: list[UserMessage],
    tools: list[AgentTool],
    reasoning_effort: str | None = None,
    reasoning_effort_parameter: str = "reasoning_effort",
    thinking_format: str = "openai",
    compat: Mapping[str, JSONValue] | None = None,
    max_tokens: int | None = None,
    include_reasoning_effort_none: bool = False,
) -> dict[str, JSONValue]:
    resolved_compat = dict(compat or {})
    supports_store = bool(resolved_compat.get("supportsStore", True))
    supports_usage = bool(resolved_compat.get("supportsUsageInStreaming", True))
    supports_reasoning_effort = bool(resolved_compat.get("supportsReasoningEffort", True))
    max_tokens_field = _string_compat(
        resolved_compat.get("maxTokensField"), default="max_completion_tokens"
    )
    user_messages: list[JSONValue] = [
        {"role": "user", "content": message.content} for message in messages
    ]
    payload: dict[str, JSONValue] = {
        "model": model,
        "stream": True,
        "messages": [
            {"role": "system", "content": system},
            *user_messages,
        ],
    }
    if supports_usage:
        payload["stream_options"] = {"include_usage": True}
    if supports_store:
        payload["store"] = False
    if max_tokens is not None:
        payload["max_tokens" if max_tokens_field == "max_tokens" else "max_completion_tokens"] = (
            max_tokens
        )
    openrouter_provider = resolved_compat.get("openrouterProvider")
    if isinstance(openrouter_provider, dict):
        payload["provider"] = openrouter_provider
    _apply_chat_reasoning(
        payload,
        reasoning_effort=reasoning_effort if supports_reasoning_effort else None,
        reasoning_effort_parameter=reasoning_effort_parameter,
        thinking_format=thinking_format,
        include_reasoning_effort_none=include_reasoning_effort_none,
    )
    if tools:
        payload["tools"] = [_tool_to_openai(tool, resolved_compat) for tool in tools]
        if resolved_compat.get("zaiToolStream") is True:
            payload["tool_stream"] = True
    return payload


def _apply_chat_reasoning(
    payload: dict[str, JSONValue],
    *,
    reasoning_effort: str | None,
    reasoning_effort_parameter: str,
    thinking_format: str,
    include_reasoning_effort_none: bool,
) -> None:
    reasoning_enabled = reasoning_effort is not None and reasoning_effort != "none"
    if thinking_format in {"zai", "qwen"}:
        payload["enable_thinking"] = reasoning_enabled
        return
    if thinking_format == "qwen-chat-template":
        payload["chat_template_kwargs"] = {
            "enable_thinking": reasoning_enabled,
            "preserve_thinking": True,
        }
        return
    if thinking_format == "deepseek":
        payload["thinking"] = {"type": "enabled" if reasoning_enabled else "disabled"}
        if reasoning_enabled:
            payload["reasoning_effort"] = reasoning_effort
        return
    if thinking_format == "openrouter" or reasoning_effort_parameter == "reasoning.effort":
        if reasoning_enabled:
            payload["reasoning"] = {"effort": reasoning_effort}
        elif include_reasoning_effort_none:
            payload["reasoning"] = {"effort": "none"}
        return
    if thinking_format == "together":
        payload["reasoning"] = {"enabled": reasoning_enabled}
        if reasoning_enabled:
            payload["reasoning_effort"] = reasoning_effort
        return
    if reasoning_enabled or include_reasoning_effort_none:
        payload["reasoning_effort"] = reasoning_effort or "none"


def _string_compat(value: object, *, default: str) -> str:
    return value if isinstance(value, str) and value else default


def _build_responses_payload(
    *,
    model: str,
    system: str,
    messages: list[UserMessage],
    tools: list[AgentTool],
    reasoning_effort: str | None = None,
    max_tokens: int | None = None,
    compat: Mapping[str, JSONValue] | None = None,
) -> dict[str, JSONValue]:
    payload: dict[str, JSONValue] = {
        "model": model,
        "stream": True,
        # Stay stateless: each turn is a fresh request, so there is no need for
        # server-side retention. ``store: false`` also keeps the path usable for
        # zero-data-retention orgs, which reject ``store: true``.
        "store": False,
        "instructions": system,
        "input": [{"role": "user", "content": message.content} for message in messages],
    }
    if max_tokens is not None:
        payload["max_output_tokens"] = max_tokens
    effort = _normalize_responses_effort(reasoning_effort)
    if effort is not None:
        # ``summary: auto`` streams ``response.reasoning_summary_text.delta``
        # events so the agent's thinking is visible, mirroring the reasoning
        # deltas surfaced on the chat-completions path.
        payload["reasoning"] = {"effort": effort, "summary": "auto"}
    if tools:
        payload["tools"] = [_tool_to_responses(tool, compat) for tool in tools]
    return payload


def _normalize_responses_effort(reasoning_effort: str | None) -> str | None:
    """Map an internal reasoning level to a Responses-API effort, or drop it."""
    if reasoning_effort is None:
        return None
    normalized = reasoning_effort.strip().lower()
    if normalized in ("", "none"):
        return None
    return normalized


def _tool_to_responses(
    tool: AgentTool, compat: Mapping[str, JSONValue] | None = None
) -> dict[str, JSONValue]:
    supports_strict_mode = (compat or {}).get("supportsStrictMode") is not False
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": dict(tool.parameters),
        **({"strict": False} if supports_strict_mode else {}),
    }


def _responses_item_key(output_index: object, item_id: object) -> str | None:
    """Key the builder that accumulates one streamed `function_call` output item.

    `output_index` is the item's position in the response and is carried by
    every function-call event, which makes it the stable identity. The item id
    is not: GitHub Copilot re-encrypts it on each event, so keying by it splits
    a single call across one builder per event -- the arguments in one, the
    name and call id in another.
    """
    if isinstance(output_index, int) and not isinstance(output_index, bool):
        return f"index:{output_index}"
    if isinstance(item_id, str) and item_id:
        return f"item:{item_id}"
    return None


def _ordered_builders(
    builders: dict[str, _ResponsesToolCallBuilder],
) -> list[_ResponsesToolCallBuilder]:
    return [
        builder for _, builder in sorted(builders.items(), key=lambda pair: pair[1].output_index)
    ]


def _responses_finish_reason(chunk: Mapping[str, Any]) -> str | None:
    response = chunk.get("response")
    if isinstance(response, Mapping):
        status = response.get("status")
        if isinstance(status, str):
            return status
    return None


def _responses_failure_event(chunk: Mapping[str, Any]) -> ProviderErrorEvent:
    return ProviderErrorEvent(message=_responses_error_message(chunk), data={"event": dict(chunk)})


def _responses_error_message(chunk: Mapping[str, Any]) -> str:
    code, message = _stream_error_details(chunk)
    return message or code or "Provider response failed"


def _stream_error_details(event: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Extract the machine code and human message from a Codex stream error.

    Codex reports failures either as a top-level ``error`` SSE event with a
    nested ``error`` object (``{"type":"error","error":{"code":...}}``) or
    as ``response.failed`` with the failure under ``response.error``. Looking
    only at top-level fields hides details such as ``server_is_overloaded``
    behind a generic fallback message.
    """
    sources: list[Mapping[str, Any]] = [event]
    nested = event.get("error")
    if isinstance(nested, Mapping):
        sources.append(nested)
    response = event.get("response")
    if isinstance(response, Mapping):
        response_error = response.get("error")
        if isinstance(response_error, Mapping):
            sources.append(response_error)

    message: str | None = None
    code: str | None = None
    for source in sources:
        if message is None:
            raw_message = source.get("message")
            if isinstance(raw_message, str) and raw_message:
                message = raw_message
        if code is None:
            raw_code = source.get("code")
            if isinstance(raw_code, str) and raw_code:
                code = raw_code
    if code is None and isinstance(nested, Mapping):
        nested_type = nested.get("type")
        if isinstance(nested_type, str) and nested_type:
            code = nested_type
    return code, message


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _tool_to_openai(
    tool: AgentTool, compat: Mapping[str, JSONValue] | None = None
) -> dict[str, JSONValue]:
    supports_strict_mode = (compat or {}).get("supportsStrictMode") is not False
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.parameters),
            **({"strict": False} if supports_strict_mode else {}),
        },
    }


def _parse_chunk_usage(raw: Mapping[str, Any]) -> Usage:
    """Parse an OpenAI-compatible ``usage`` payload into a Usage.

    Ports Pi's openai-completions.ts parseChunkUsage: ``cached_tokens`` are
    cache reads, writes are subtracted from the prompt to leave the fresh input,
    and ``completion_tokens`` already includes reasoning tokens. Cost is left
    unset (None) because Rio has no per-model pricing table.
    """
    prompt_tokens = int_or_zero(raw.get("prompt_tokens"))
    prompt_details = raw.get("prompt_tokens_details")
    cached_tokens: int | None = None
    cache_write = 0
    if isinstance(prompt_details, Mapping):
        cached_tokens = int_or_none(prompt_details.get("cached_tokens"))
        cache_write = int_or_zero(prompt_details.get("cache_write_tokens"))
    # Nullish fallback, matching Pi's `cached_tokens ?? prompt_cache_hit_tokens
    # ?? 0` (DeepSeek reports cache hits in prompt_cache_hit_tokens): a reported
    # 0 does not fall through.
    if cached_tokens is None:
        cached_tokens = int_or_none(raw.get("prompt_cache_hit_tokens"))
    cache_read = cached_tokens or 0
    fresh_input = max(0, prompt_tokens - cache_read - cache_write)
    output = int_or_zero(raw.get("completion_tokens"))
    reasoning = None
    completion_details = raw.get("completion_tokens_details")
    if isinstance(completion_details, Mapping):
        reasoning = int_or_zero(completion_details.get("reasoning_tokens"))
    return Usage(
        input=fresh_input,
        output=output,
        cache_read=cache_read,
        cache_write=cache_write,
        reasoning=reasoning,
        total_tokens=fresh_input + output + cache_read + cache_write,
    )


def _usage_from_responses_event(chunk: Mapping[str, Any]) -> Usage | None:
    """Parse billed usage from a `/v1/responses` terminal event.

    Cache reads and writes are subtracted from input_tokens to leave fresh input.
    """
    response = chunk.get("response")
    if not isinstance(response, Mapping):
        return None
    raw = response.get("usage")
    if not isinstance(raw, Mapping):
        return None
    input_details = raw.get("input_tokens_details")
    cache_read = (
        int_or_zero(input_details.get("cached_tokens")) if isinstance(input_details, Mapping) else 0
    )
    cache_write = (
        int_or_zero(input_details.get("cache_write_tokens"))
        if isinstance(input_details, Mapping)
        else 0
    )
    output_details = raw.get("output_tokens_details")
    # Leave reasoning None (not 0) when the provider reports no breakdown,
    # honoring the "None = not reported" contract on Usage.
    reasoning = (
        int_or_zero(output_details.get("reasoning_tokens"))
        if isinstance(output_details, Mapping)
        else None
    )
    return Usage(
        input=max(0, int_or_zero(raw.get("input_tokens")) - cache_read - cache_write),
        output=int_or_zero(raw.get("output_tokens")),
        cache_read=cache_read,
        cache_write=cache_write,
        reasoning=reasoning,
        total_tokens=int_or_zero(raw.get("total_tokens")),
    )


def _tool_call_deltas(delta: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    tool_calls = delta.get("tool_calls") or delta.get("toolCalls")
    if not isinstance(tool_calls, list):
        return []
    return [tool_call for tool_call in tool_calls if isinstance(tool_call, Mapping)]


def _thinking_delta(delta: Mapping[str, Any]) -> str | None:
    for field_name in ("reasoning_content", "reasoning", "thinking"):
        value = delta.get(field_name)
        if isinstance(value, str) and value:
            return value
    return None
