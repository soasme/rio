"""Shared retry loop for provider adapters."""

from __future__ import annotations

from asyncio import sleep
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from rio.ai._provider_events import ProviderErrorEvent, ProviderEvent, ProviderRetryEvent
from rio.ai.types import JSONValue

RETRY_BASE_DELAY_SECONDS = 0.25


@dataclass(slots=True)
class RetryAttempt:
    """State of one provider request attempt, shared with the retry loop."""

    number: int = 0
    # Set once model output reached the caller; later failures are not retried.
    emitted_content: bool = False


class RetryableFailure(Exception):
    """Raised by an attempt to ask the retry loop for another attempt.

    ``error`` is emitted instead when the retry budget is spent or content was
    already emitted.
    """

    def __init__(
        self,
        reason: str,
        error: ProviderErrorEvent,
        *,
        data: dict[str, JSONValue] | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.error = error
        self.data = data
        self.retry_after_seconds = retry_after_seconds


async def stream_with_retries(
    stream_attempt: Callable[[RetryAttempt], AsyncGenerator[ProviderEvent, None]],
    *,
    max_retries: int,
    max_retry_delay_seconds: float,
) -> AsyncIterator[ProviderEvent]:
    """Run ``stream_attempt`` until it finishes, retrying transient failures.

    An attempt asks for a retry by raising ``RetryableFailure``. Network errors
    (``httpx.HTTPError``) are retried the same way. Neither is retried once the
    attempt set ``emitted_content`` or after ``max_retries`` retries.
    """
    attempt = RetryAttempt()
    while True:
        retry_after: float | None = None
        try:
            async with aclosing(stream_attempt(attempt)) as events:
                async for event in events:
                    yield event
            return
        except RetryableFailure as failure:
            if attempt.emitted_content or attempt.number >= max_retries:
                yield failure.error
                return
            reason = failure.reason
            data = failure.data
            retry_after = failure.retry_after_seconds
        except httpx.HTTPError as exc:
            if attempt.emitted_content or attempt.number >= max_retries:
                yield ProviderErrorEvent(message=str(exc), data={"attempts": attempt.number + 1})
                return
            reason = "network error"
            data = {"error": str(exc), "error_type": type(exc).__name__}

        delay = retry_delay_seconds(attempt.number, max_delay_seconds=max_retry_delay_seconds)
        if retry_after is not None:
            delay = max(0.0, min(retry_after, max_retry_delay_seconds))
        yield provider_retry_event(
            attempt=attempt.number,
            max_retries=max_retries,
            delay_seconds=delay,
            reason=reason,
            data=data,
        )
        await sleep(delay)
        attempt = RetryAttempt(number=attempt.number + 1)


async def check_response_status(
    response: httpx.Response,
    error: Callable[[str], ProviderErrorEvent],
    *,
    is_retryable: Callable[[int, str], bool] | None = None,
) -> ProviderErrorEvent | None:
    """Return ``None`` for a successful response, else handle its HTTP error.

    Raises ``RetryableFailure`` for a transient status, honoring ``Retry-After``.
    Otherwise returns the terminal event ``error`` builds from the response body.
    ``is_retryable(status_code, body)`` overrides the default transient statuses.
    """
    status_code = response.status_code
    if status_code < 400:
        return None
    body = (await response.aread()).decode(errors="replace")
    event = error(body)
    retryable = (
        is_retryable(status_code, body) if is_retryable else is_transient_status(status_code)
    )
    if retryable:
        raise RetryableFailure(
            f"HTTP {status_code}",
            event,
            data={"status_code": status_code, "body": body},
            retry_after_seconds=parse_retry_after(response.headers.get("retry-after")),
        )
    return event


def is_transient_status(status_code: int) -> bool:
    """Return whether an HTTP status is worth retrying."""
    return status_code in {408, 409, 425, 429} or status_code >= 500


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Return the delay a ``Retry-After`` header asks for, in seconds.

    Accepts delay-seconds or an HTTP-date. Returns ``None`` when absent or invalid.
    """
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


def retry_delay_seconds(attempt: int, *, max_delay_seconds: float) -> float:
    """Return an exponential retry delay capped by provider config."""
    if max_delay_seconds <= 0:
        return 0.0
    base_delay = min(RETRY_BASE_DELAY_SECONDS, max_delay_seconds)
    return float(min(max_delay_seconds, base_delay * (2**attempt)))


def provider_retry_event(
    *,
    attempt: int,
    max_retries: int,
    delay_seconds: float,
    reason: str,
    data: dict[str, JSONValue] | None = None,
) -> ProviderRetryEvent:
    """Build a provider-neutral retry progress event."""
    next_attempt = attempt + 2
    max_attempts = max_retries + 1
    delay_suffix = f" in {delay_seconds:g}s" if delay_seconds else ""
    return ProviderRetryEvent(
        attempt=next_attempt,
        max_attempts=max_attempts,
        delay_seconds=delay_seconds,
        message=(
            f"Retrying provider request {next_attempt}/{max_attempts} after {reason}{delay_suffix}."
        ),
        data=data,
    )
