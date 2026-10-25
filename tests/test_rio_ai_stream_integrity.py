"""Provider completion, truncation, authentication and usage regressions."""

from __future__ import annotations

import json

import httpx
import pytest

from rio.ai._provider_events import (
    ProviderEvent,
    ProviderToolCallDeltaEvent,
    ProviderToolCallEvent,
)
from rio.ai.anthropic import AnthropicProvider
from rio.ai.env import AnthropicConfig, OpenAICompatibleConfig
from rio.ai.google import GoogleGenerativeAIConfig, GoogleGenerativeAIProvider
from rio.ai.messages import AssistantMessage
from rio.ai.mistral import MistralConfig, MistralConversationsProvider
from rio.ai.openai_compatible import OpenAICompatibleProvider
from rio.ai.provider import ModelProvider


def sse(*chunks: object) -> str:
    return "".join("data: " + (c if isinstance(c, str) else json.dumps(c)) + "\n\n" for c in chunks)


def provider(kind: str, client: httpx.AsyncClient) -> ModelProvider:
    if kind == "anthropic":
        return AnthropicProvider(
            AnthropicConfig(api_key="secret-api-key", max_retries=1, max_retry_delay_seconds=0),
            client=client,
        )
    if kind == "google":
        return GoogleGenerativeAIProvider(
            GoogleGenerativeAIConfig(
                api_key="secret-api-key", max_retries=1, max_retry_delay_seconds=0
            ),
            client=client,
        )
    if kind == "mistral":
        return MistralConversationsProvider(
            MistralConfig(api_key="secret-api-key", max_retries=1, max_retry_delay_seconds=0),
            client=client,
        )
    return OpenAICompatibleProvider(
        OpenAICompatibleConfig(
            api_key="secret-api-key",
            max_retries=1,
            max_retry_delay_seconds=0,
            api="openai-responses" if kind == "responses" else "openai-completions",
        ),
        client=client,
    )


async def collect(kind: str, body: str) -> tuple[AssistantMessage, list[httpx.Request]]:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        message = await provider(kind, client).complete(
            model="test-model", system="", messages=[], tools=[]
        )
    return message, requests


@pytest.mark.parametrize("kind", ["anthropic", "chat", "mistral", "google"])
@pytest.mark.parametrize("partial", [False, True])
async def test_clean_eof_without_finish_is_error(kind: str, partial: bool) -> None:
    chunk = {
        "anthropic": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "partial"},
        },
        "google": {"candidates": [{"content": {"parts": [{"text": "partial"}]}}]},
        "chat": {"choices": [{"delta": {"content": "partial"}}]},
        "mistral": {"choices": [{"delta": {"content": "partial"}}]},
    }[kind]
    message, requests = await collect(kind, sse(chunk) if partial else "")
    assert message.stop_reason == "error"
    assert len(requests) == (1 if partial else 2)


@pytest.mark.parametrize("kind", ["chat", "mistral"])
async def test_done_sentinel_without_finish_reason_is_error(kind: str) -> None:
    message, _ = await collect(kind, sse("[DONE]"))
    assert message.stop_reason == "error"


@pytest.mark.parametrize("kind", ["chat", "mistral", "google", "responses"])
async def test_token_limit_with_tool_call_reports_length(kind: str) -> None:
    call = {"index": 0, "id": "call-1", "function": {"name": "step", "arguments": "{}"}}
    body = sse(
        {"choices": [{"delta": {"tool_calls": [call]}, "finish_reason": "length"}]}, "[DONE]"
    )
    if kind == "google":
        body = sse(
            {
                "candidates": [
                    {
                        "content": {"parts": [{"functionCall": {"name": "step", "args": {}}}]},
                        "finishReason": "MAX_TOKENS",
                    }
                ]
            }
        )
    elif kind == "responses":
        body = sse(
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "step",
                    "arguments": "{}",
                },
            },
            {"type": "response.incomplete", "response": {"status": "incomplete"}},
        )
    message, _ = await collect(kind, body)
    assert message.tool_calls
    assert message.stop_reason == "length"


async def test_google_usage_only_chunk_and_header_auth() -> None:
    message, requests = await collect(
        "google",
        sse(
            {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]},
            {
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "cachedContentTokenCount": 30,
                    "candidatesTokenCount": 20,
                    "thoughtsTokenCount": 10,
                    "totalTokenCount": 130,
                }
            },
        ),
    )
    usage = message.usage
    assert (usage.input, usage.cache_read, usage.output, usage.reasoning, usage.total_tokens) == (
        70,
        30,
        30,
        10,
        130,
    )
    assert requests[0].headers["x-goog-api-key"] == "secret-api-key"
    assert "key" not in requests[0].url.params
    assert "secret-api-key" not in str(requests[0].url)


async def test_mistral_requests_and_parses_trailing_usage() -> None:
    message, requests = await collect(
        "mistral",
        sse(
            {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
            "[DONE]",
        ),
    )
    usage = message.usage
    assert (usage.input, usage.output, usage.total_tokens) == (100, 20, 120)
    assert json.loads(requests[0].content)["stream_options"] == {"include_usage": True}


@pytest.mark.parametrize("kind", ["anthropic", "chat", "mistral", "responses"])
async def test_tool_call_arguments_stream_before_the_final_call(kind: str) -> None:
    parts = ['{"ops": [', "]}"]
    if kind == "anthropic":
        chunks: list[object] = [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "call-1", "name": "step"},
            },
            *(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": part},
                }
                for part in parts
            ),
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        ]
    elif kind == "responses":
        item = {"type": "function_call", "id": "fc-1", "call_id": "call-1", "name": "step"}
        chunks = [
            {"type": "response.output_item.added", "output_index": 0, "item": item},
            *(
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 0,
                    "item_id": "fc-1",
                    "delta": part,
                }
                for part in parts
            ),
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {**item, "arguments": "".join(parts)},
            },
            {"type": "response.completed", "response": {"status": "completed"}},
        ]
    else:
        calls = [
            {"index": 0, "id": "call-1", "function": {"name": "step", "arguments": parts[0]}},
            {"index": 0, "function": {"arguments": parts[1]}},
        ]
        chunks = [
            *({"choices": [{"delta": {"tool_calls": [call]}}]} for call in calls),
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            "[DONE]",
        ]
    events: list[ProviderEvent] = []

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse(*chunks))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        message = await provider(kind, client).complete(
            model="test-model", system="", messages=[], tools=[], on_event=events.append
        )
    deltas = [e.delta for e in events if isinstance(e, ProviderToolCallDeltaEvent)]
    assert deltas == parts
    final = [i for i, e in enumerate(events) if isinstance(e, ProviderToolCallEvent)]
    assert final and final[0] > max(
        i for i, e in enumerate(events) if isinstance(e, ProviderToolCallDeltaEvent)
    )
    assert message.tool_calls[0].arguments == {"ops": []}
