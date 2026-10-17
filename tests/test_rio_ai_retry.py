"""Tests for the shared provider retry loop in `rio.ai.retry`."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime

import httpx

from rio.ai._provider_events import (
    ProviderErrorEvent,
    ProviderEvent,
    ProviderRetryEvent,
    ProviderTextDeltaEvent,
)
from rio.ai.env import OpenAICompatibleConfig
from rio.ai.messages import UserMessage
from rio.ai.openai_compatible import OpenAICompatibleProvider
from rio.ai.retry import RetryableFailure, RetryAttempt, parse_retry_after, stream_with_retries


async def _collect(
    stream_attempt: Callable[[RetryAttempt], AsyncGenerator[ProviderEvent, None]],
    *,
    max_retries: int = 2,
    max_delay: float = 0.0,
) -> list[ProviderEvent]:
    return [
        event
        async for event in stream_with_retries(
            stream_attempt,
            max_retries=max_retries,
            max_retry_delay_seconds=max_delay,
        )
    ]


def _failing_attempts(
    failures: list[Exception], *, emit_first: bool = False
) -> tuple[Callable[[RetryAttempt], AsyncGenerator[ProviderEvent, None]], list[int]]:
    attempts: list[int] = []

    async def stream_attempt(attempt: RetryAttempt) -> AsyncGenerator[ProviderEvent, None]:
        attempts.append(attempt.number)
        if emit_first:
            attempt.emitted_content = True
            yield ProviderTextDeltaEvent(delta="partial")
        if attempt.number < len(failures):
            raise failures[attempt.number]
        yield ProviderTextDeltaEvent(delta="done")

    return stream_attempt, attempts


def _failure(reason: str = "HTTP 503") -> RetryableFailure:
    return RetryableFailure(
        reason, ProviderErrorEvent(message="terminal"), data={"status_code": 503}
    )


async def test_retries_retryable_failure_then_streams_attempt() -> None:
    stream_attempt, attempts = _failing_attempts([_failure()])

    events = await _collect(stream_attempt)

    assert attempts == [0, 1]
    assert events == [
        ProviderRetryEvent(
            attempt=2,
            max_attempts=3,
            delay_seconds=0.0,
            message="Retrying provider request 2/3 after HTTP 503.",
            data={"status_code": 503},
        ),
        ProviderTextDeltaEvent(delta="done"),
    ]


async def test_emits_failure_error_when_retries_are_spent() -> None:
    stream_attempt, attempts = _failing_attempts([_failure(), _failure(), _failure()])

    events = await _collect(stream_attempt)

    assert attempts == [0, 1, 2]
    assert [event.type for event in events] == ["retry", "retry", "error"]
    assert events[-1] == ProviderErrorEvent(message="terminal")


async def test_does_not_retry_after_content_was_emitted() -> None:
    stream_attempt, attempts = _failing_attempts(
        [_failure(), httpx.ReadError("boom")], emit_first=True
    )

    events = await _collect(stream_attempt)

    assert attempts == [0]
    assert events == [
        ProviderTextDeltaEvent(delta="partial"),
        ProviderErrorEvent(message="terminal"),
    ]


async def test_retries_network_errors_and_reports_attempts_when_spent() -> None:
    stream_attempt, attempts = _failing_attempts(
        [httpx.ConnectError("refused")] * 3,
    )

    events = await _collect(stream_attempt)

    assert attempts == [0, 1, 2]
    assert events[0] == ProviderRetryEvent(
        attempt=2,
        max_attempts=3,
        delay_seconds=0.0,
        message="Retrying provider request 2/3 after network error.",
        data={"error": "refused", "error_type": "ConnectError"},
    )
    assert events[-1] == ProviderErrorEvent(message="refused", data={"attempts": 3})


async def test_network_error_after_content_is_not_retried() -> None:
    stream_attempt, attempts = _failing_attempts([httpx.ReadError("boom")], emit_first=True)

    events = await _collect(stream_attempt)

    assert attempts == [0]
    assert events[-1] == ProviderErrorEvent(message="boom", data={"attempts": 1})


def _sse_response(
    status_code: int, body: str, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(status_code, content=body.encode(), headers=headers)


async def _stream_openai(
    responses: list[httpx.Response], *, max_delay: float = 0.0, max_retries: int = 2
) -> list[ProviderEvent]:
    calls = iter(responses)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: next(calls)))
    provider = OpenAICompatibleProvider(
        OpenAICompatibleConfig(
            api_key="key", max_retries=max_retries, max_retry_delay_seconds=max_delay
        ),
        client=client,
    )
    return [
        event
        async for event in provider._stream_provider_events(
            model="model",
            system="",
            messages=[UserMessage(content="hi")],
            tools=[],
        )
    ]


_CHAT_OK = 'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n'


async def test_adapter_retries_transient_http_status() -> None:
    events = await _stream_openai([_sse_response(503, "busy"), _sse_response(200, _CHAT_OK)])

    assert events[0] == ProviderRetryEvent(
        attempt=2,
        max_attempts=3,
        delay_seconds=0.0,
        message="Retrying provider request 2/3 after HTTP 503.",
        data={"status_code": 503, "body": "busy"},
    )
    assert [event.type for event in events[1:]] == ["response_start", "text_delta", "response_end"]


async def test_adapter_reports_terminal_http_status_without_retry() -> None:
    events = await _stream_openai([_sse_response(400, '{"error":{"message":"bad"}}')])

    assert events == [
        ProviderErrorEvent(
            message="OpenAI-compatible provider request failed with status 400 for model "
            "model: bad",
            data={"status_code": 400, "body": '{"error":{"message":"bad"}}', "attempts": 1},
        )
    ]


async def test_retry_after_reaches_the_error_when_transport_retries_are_off() -> None:
    """A caller that owns retries, such as the durable runner, still sees Retry-After."""
    events = await _stream_openai(
        [_sse_response(429, "slow down", headers={"retry-after": "30"})], max_retries=0
    )

    assert len(events) == 1
    assert isinstance(events[0], ProviderErrorEvent)
    assert events[0].data is not None
    assert events[0].data["status_code"] == 429
    assert events[0].data["retry_after_seconds"] == 30.0


def test_parse_retry_after_accepts_seconds_and_http_dates() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    assert parse_retry_after("7") == 7.0
    assert parse_retry_after(" 0 ") == 0.0
    assert parse_retry_after("Thu, 01 Jan 2026 12:00:30 GMT", now=now) == 30.0
    assert parse_retry_after("Thu, 01 Jan 2026 11:59:00 GMT", now=now) == 0.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None
    assert parse_retry_after("-5") is None


async def test_adapter_honors_retry_after_clamped_to_max_delay() -> None:
    events = await _stream_openai(
        [
            _sse_response(429, "slow", headers={"retry-after": "0"}),
            _sse_response(503, "busy", headers={"retry-after": "120"}),
            _sse_response(200, _CHAT_OK),
        ],
        max_delay=0.05,
    )

    assert [
        event.delay_seconds for event in events[:2] if isinstance(event, ProviderRetryEvent)
    ] == [0.0, 0.05]
    assert events[2].type == "response_start"


async def test_adapter_falls_back_to_exponential_delay_without_retry_after() -> None:
    events = await _stream_openai(
        [
            _sse_response(503, "busy", headers={"retry-after": "later"}),
            _sse_response(200, _CHAT_OK),
        ],
        max_delay=0.01,
    )

    assert isinstance(events[0], ProviderRetryEvent)
    assert events[0].delay_seconds == 0.01
