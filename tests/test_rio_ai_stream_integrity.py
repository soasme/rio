"""Provider completion, truncation, authentication and usage regressions."""

import json

import httpx
import pytest

from rio.ai.anthropic import AnthropicProvider
from rio.ai.env import AnthropicConfig, OpenAICompatibleConfig
from rio.ai.google import GoogleGenerativeAIProvider
from rio.ai.mistral import MistralConversationsProvider
from rio.ai.openai_compatible import OpenAICompatibleProvider


def sse(*chunks):
    return "".join("data: " + (c if isinstance(c, str) else json.dumps(c)) + "\n\n" for c in chunks)


def provider(kind, client):
    config = dict(api_key="secret-api-key", max_retries=1, max_retry_delay_seconds=0)
    if kind == "anthropic":
        return AnthropicProvider(AnthropicConfig(**config), client=client)
    cls = {
        "chat": OpenAICompatibleProvider,
        "google": GoogleGenerativeAIProvider,
        "mistral": MistralConversationsProvider,
        "responses": OpenAICompatibleProvider,
    }[kind]
    return cls(
        OpenAICompatibleConfig(
            **config, api="openai-responses" if kind == "responses" else "openai-completions"
        ),
        client=client,
    )


async def collect(kind, body):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        events = [
            e
            async for e in provider(kind, client).stream_response(
                model="test-model", system="", messages=[], tools=[]
            )
        ]
    return events, requests


@pytest.mark.parametrize("kind", ["anthropic", "chat", "mistral", "google"])
@pytest.mark.parametrize("partial", [False, True])
async def test_clean_eof_without_finish_is_error(kind, partial):
    chunk = {
        "anthropic": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "partial"},
        },
        "google": {"candidates": [{"content": {"parts": [{"text": "partial"}]}}]},
        "chat": {"choices": [{"delta": {"content": "partial"}}]},
        "mistral": {"choices": [{"delta": {"content": "partial"}}]},
    }[kind]
    events, requests = await collect(kind, sse(chunk) if partial else "")
    assert events[-1].type == "error"
    assert not any(e.type == "done" for e in events)
    assert len(requests) == (1 if partial else 2)


@pytest.mark.parametrize("kind", ["chat", "mistral"])
async def test_done_sentinel_without_finish_reason_is_error(kind):
    events, _ = await collect(kind, sse("[DONE]"))
    assert events[-1].type == "error"


@pytest.mark.parametrize("kind", ["chat", "mistral", "google", "responses"])
async def test_token_limit_with_tool_call_reports_length(kind):
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
    events, _ = await collect(kind, body)
    assert events[-1].type == "done"
    assert events[-1].message.tool_calls
    assert events[-1].message.stop_reason == "length"


async def test_google_usage_only_chunk_and_header_auth():
    events, requests = await collect(
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
    usage = events[-1].message.usage
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


async def test_mistral_requests_and_parses_trailing_usage():
    events, requests = await collect(
        "mistral",
        sse(
            {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
            "[DONE]",
        ),
    )
    usage = events[-1].message.usage
    assert (usage.input, usage.output, usage.total_tokens) == (100, 20, 120)
    assert json.loads(requests[0].content)["stream_options"] == {"include_usage": True}
