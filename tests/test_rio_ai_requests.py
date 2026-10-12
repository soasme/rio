"""Transport and final-response contracts shared by the provider adapters."""

import json

import httpx
import pytest

from rio.ai import (
    AnthropicConfig,
    AnthropicProvider,
    GoogleGenerativeAIProvider,
    MistralConversationsProvider,
    OpenAICodexConfig,
    OpenAICodexCredentials,
    OpenAICodexProvider,
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
)

KINDS = ["anthropic", "google", "mistral", "openai", "responses", "codex"]


def sse(*events):
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()


class Body(httpx.AsyncByteStream):
    def __init__(self, data, *, fail=False):
        self.data = data
        self.fail = fail
        self.closed = False

    async def __aiter__(self):
        yield self.data
        if self.fail:
            raise httpx.ReadError("connection lost")

    async def aclose(self):
        self.closed = True


def provider(kind, client, **kwargs):
    common = dict(max_retries=1, max_retry_delay_seconds=0, **kwargs)
    if kind == "codex":

        async def credentials():
            return OpenAICodexCredentials(access_token="token", account_id="account")

        return OpenAICodexProvider(
            OpenAICodexConfig(credential_resolver=credentials, **common), client=client
        )
    if kind == "anthropic":
        return AnthropicProvider(AnthropicConfig(api_key="token", **common), client=client)
    cls = {"google": GoogleGenerativeAIProvider, "mistral": MistralConversationsProvider}.get(
        kind, OpenAICompatibleProvider
    )
    config = OpenAICompatibleConfig(
        api_key="token",
        api="openai-responses" if kind == "responses" else "openai-completions",
        **common,
    )
    return cls(config, client=client)


def success(kind):
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


async def collect(adapter):
    return [
        event
        async for event in adapter.stream_response(model="m", system="s", messages=[], tools=[])
    ]


@pytest.mark.parametrize("kind", KINDS)
async def test_shared_request_retries_then_closes_before_publishing(kind):
    responses = []
    body = Body(success(kind))

    def handle(request):
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
        async for event in provider(kind, client).stream_response(
            model="m", system="s", messages=[], tools=[]
        ):
            assert body.closed
            assert event.type == "done"
            assert event.message.text == "hi"
        assert len(responses) == 2
        assert all(response.is_closed for response in responses)


@pytest.mark.parametrize("kind", KINDS)
async def test_shared_request_terminal_http_error_is_not_retried(kind):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        events = await collect(provider(kind, client))
    assert len(requests) == len(events) == 1
    assert events[0].type == "error"
    assert "bad request" in events[0].error.error_message
    assert events[0].error.diagnostics[0].details["attempts"] == 1


@pytest.mark.parametrize("kind", KINDS)
async def test_network_failure_after_text_is_not_retried(kind):
    requests = []
    # Stop before the terminal event so Responses still reads the failed transport.
    data = success(kind).split(b"\n\n")[0] + b"\n\n"
    body = Body(data, fail=True)

    def handle(request):
        requests.append(request)
        return httpx.Response(200, stream=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        events = await collect(provider(kind, client))
    assert len(requests) == len(events) == 1
    assert events[0].type == "error"
    assert events[0].error.text == "hi"
    assert body.closed


async def test_codex_refreshes_credentials_and_discards_reasoning_on_network_retry():
    calls = []
    attempts = []

    async def credentials():
        calls.append(len(calls))
        return OpenAICodexCredentials(access_token=f"token-{len(calls)}", account_id="account")

    def handle(request):
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
        events = await collect(adapter)
    assert [request.headers["authorization"] for request in attempts] == [
        "Bearer token-1",
        "Bearer token-2",
    ]
    assert len(events) == 1
    assert events[0].message.text == "hi"
    assert [block.type for block in events[0].message.content] == ["text"]


async def test_response_header_observer_failure_does_not_lose_message():
    def observer(headers):
        assert headers["x-provider"] == "backing-provider"
        raise RuntimeError("observer failed")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, content=success("openai"), headers={"x-provider": "backing-provider"}
            )
        )
    ) as client:
        events = await collect(
            provider(
                "openai",
                client,
                response_provider_header="x-provider",
                response_headers_observer=observer,
            )
        )
    assert len(events) == 1
    assert events[0].message.text == "hi"
    assert events[0].message.response_provider == "backing-provider"
    assert events[0].message.diagnostics


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
async def test_codex_stream_error_after_output_is_not_retried(output):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200, content=sse(output, {"type": "error", "error": {"code": "server_error"}})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        events = await collect(provider("codex", client))
    assert len(requests) == len(events) == 1
    assert events[0].type == "error"
    assert events[0].error.error_message == "server_error"
    assert events[0].error.content


async def test_task_cancellation_closes_the_response():
    import asyncio

    started = asyncio.Event()

    class BlockingBody(Body):
        async def __aiter__(self):
            started.set()
            await asyncio.Event().wait()
            yield b""

    body = BlockingBody(b"")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body))
    ) as client:
        task = asyncio.create_task(collect(provider("openai", client)))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert body.closed
