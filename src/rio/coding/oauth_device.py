"""RFC 8628-style device authorization polling helpers."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from rio.coding.oauth import OAuthError

DevicePollStatus = Literal["complete", "pending", "slow_down", "failed"]


@dataclass(frozen=True, slots=True)
class DevicePollResult[T]:
    """Result of one device-token polling request."""

    status: DevicePollStatus
    value: T | None = None
    message: str | None = None
    interval_seconds: float | None = None


async def poll_oauth_device_code[T](
    poll: Callable[[], Awaitable[DevicePollResult[T]]],
    *,
    interval_seconds: float | None = None,
    expires_in_seconds: float | None = None,
    wait_before_first_poll: bool = False,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> T:
    """Poll an OAuth device flow with RFC 8628 timing and cancellation."""
    interval = _poll_interval(interval_seconds)
    deadline = monotonic() + expires_in_seconds if expires_in_seconds is not None else math.inf
    if wait_before_first_poll:
        await sleep(min(interval, max(deadline - monotonic(), 0)))

    saw_slow_down = False
    while monotonic() < deadline:
        result = await poll()
        if result.status == "complete":
            if result.value is None:
                raise OAuthError("Device flow returned no credential")
            return result.value
        if result.status == "failed":
            raise OAuthError(result.message or "Device authorization failed")
        if result.status == "slow_down":
            saw_slow_down = True
            interval = (
                _poll_interval(result.interval_seconds)
                if result.interval_seconds is not None
                else interval + 5
            )

        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        await sleep(min(interval, remaining))

    suffix = " after one or more slow_down responses" if saw_slow_down else ""
    raise OAuthError(f"Device flow timed out{suffix}")


def _poll_interval(value: float | None) -> float:
    if value is None or not math.isfinite(value) or value <= 0:
        return 5
    return max(value, 1)
