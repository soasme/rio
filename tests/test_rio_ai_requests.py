"""Transport and final-response contracts shared by the provider adapters."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from rio.ai import (
    AnthropicConfig,
    AnthropicProvider,
    GoogleGenerativeAIConfig,
    GoogleGenerativeAIProvider,
    MistralConfig,
    MistralConversationsProvider,
    OpenAICodexConfig,
    OpenAICodexCredentials,
    OpenAICodexProvider,
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
)
from rio.ai.messages import AssistantMessage
from rio.ai.provider import ModelProvider

KINDS = ["anthropic", "google", "mistral", "openai", "responses", "codex"]


def sse(*events: object) -> bytes:
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()


class Body(httpx.AsyncByteStream):
    def __init__(self, data: bytes, *, fail: bool = False) -> None:
        self.data = data
        self.fail = fail
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.data
        if self.fail:
            raise httpx.ReadError("connection lost")

    async def aclose(self) -> None:
        self.closed = True


def provider(kind: str, client: httpx.AsyncClient, **kwargs: Any) -> ModelProvider:
    common = dict(max_retries=1, max_retry_delay_seconds=0, **kwargs)
    if kind == "codex":

        async def credentials() -> OpenAICodexCredentials:
            return OpenAICodexCredentials(access_token="token", account_id="account")

        return OpenAICodexProvider(
            OpenAICodexConfig(credential_resolver=credentials, **common), client=client
        )
    if kind == "anthropic":
        return AnthropicProvider(AnthropicConfig(api_key="token", **common), client=client)
    if kind == "google":
        return GoogleGenerativeAIProvider(
            GoogleGenerativeAIConfig(api_key="token", **common), client=client
        )
    if kind == "mistral":
        return MistralConversationsProvider(MistralConfig(api_key="token", **common), client=client)
    config = OpenAICompatibleConfig(
        api_key="token",
        api="openai-responses" if kind == "responses" else "openai-completions",
        **common,
    )
    return OpenAICompatibleProvider(config, client=client)


def success(kind: str) -> bytes:
    if kind == "anthropic":
        return sse(
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        )
    if kind == "google":
        return sse(
            {"candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}]}
        )
    if kind in {"responses", "codex"}:
        return sse(
            {"type": "response.output_text.delta", "delta": "hi"},
            {"type": "response.completed", "response": {"status": "completed"}},
        )
    return sse({"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]})


async def complete(adapter: ModelProvider) -> AssistantMessage:
    return await adapter.complete(model="m", system="s", messages=[], tools=[])


@pytest.mark.parametrize("kind", KINDS)
async def test_shared_request_retries_then_closes_before_publishing(kind: str) -> None:
    responses: list[httpx.Response] = []
    body = Body(success(kind))

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert json.loads(request.content)
        response = (
            httpx.Response(503, content=b"busy")
            if not responses
            else httpx.Response(200, stream=body)
        )
        responses.append(response)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        message = await complete(provider(kind, client))
        assert body.closed
        assert message.stop_reason == "stop"
        assert message.text == "hi"
        assert len(responses) == 2
        assert all(response.is_closed for response in responses)


@pytest.mark.parametrize("kind", KINDS)
async def test_shared_request_terminal_http_error_is_not_retried(kind: str) -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        message = await complete(provider(kind, client))
    assert len(requests) == 1
    assert message.stop_reason == "error"
    assert message.error_message is not None
    assert "bad request" in message.error_message
    assert message.diagnostics is not None
    assert message.diagnostics[0].details is not None
    assert message.diagnostics[0].details["attempts"] == 1


@pytest.mark.parametrize("kind", KINDS)
async def test_network_failure_after_text_is_not_retried(kind: str) -> None:
    requests = []
    # Stop before the terminal event so Responses still reads the failed transport.
    data = success(kind).split(b"\n\n")[0] + b"\n\n"
    body = Body(data, fail=True)

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, stream=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        message = await complete(provider(kind, client))
    assert len(requests) == 1
    assert message.stop_reason == "error"
    assert message.text == "hi"
    assert body.closed


async def test_codex_refreshes_credentials_and_discards_reasoning_on_network_retry() -> None:
    calls: list[int] = []
    attempts = []

    async def credentials() -> OpenAICodexCredentials:
        calls.append(len(calls))
        return OpenAICodexCredentials(access_token=f"token-{len(calls)}", account_id="account")

    def handle(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        body = (
            Body(sse({"type": "response.reasoning.delta", "delta": "discard"}), fail=True)
            if len(attempts) == 1
            else Body(success("codex"))
        )
        return httpx.Response(200, stream=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = OpenAICodexProvider(
            OpenAICodexConfig(credential_resolver=credentials, max_retry_delay_seconds=0),
            client=client,
        )
        message = await complete(adapter)
    assert [request.headers["authorization"] for request in attempts] == [
        "Bearer token-1",
        "Bearer token-2",
    ]
    assert message.text == "hi"
    assert [block.type for block in message.content] == ["text"]


@pytest.mark.parametrize(
    "output",
    [
        {"type": "response.reasoning.delta", "delta": "think"},
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "read",
                "arguments": "{}",
            },
        },
    ],
)
async def test_codex_stream_error_after_output_is_not_retried(output: str) -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, content=sse(output, {"type": "error", "error": {"code": "server_error"}})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        message = await complete(provider("codex", client))
    assert len(requests) == 1
    assert message.stop_reason == "error"
    assert message.error_message == "server_error"
    assert message.content


async def test_task_cancellation_closes_the_response() -> None:
    import asyncio

    started = asyncio.Event()

    class BlockingBody(Body):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            started.set()
            await asyncio.Event().wait()
            yield b""

    body = BlockingBody(b"")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body))
    ) as client:
        task = asyncio.create_task(complete(provider("openai", client)))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert body.closed
