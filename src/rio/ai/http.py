"""HTTP client helpers shared by Rio network integrations."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import aclosing, contextmanager
from typing import TYPE_CHECKING, Any

import httpx

from rio.ai._provider_events import ProviderErrorEvent, ProviderEvent, ProviderResponseStartEvent
from rio.ai.env import AnthropicConfig, OpenAICompatibleConfig
from rio.ai.http_errors import provider_http_error_message
from rio.ai.messages import AssistantMessage, UserMessage
from rio.ai.provider import collect_response
from rio.ai.retry import RetryAttempt, check_response_status, stream_with_retries
from rio.ai.tools import AgentTool
from rio.ai.types import JSONValue

if TYPE_CHECKING:
    from rio.ai.openai_codex import OpenAICodexConfig

_PROXY_ENV_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def normalize_proxy_url(proxy_url: str) -> str:
    """Return an httpx-compatible proxy URL.

    Some environments use ``socks://`` as a generic SOCKS proxy scheme. httpx
    accepts explicit SOCKS versions (for example ``socks5://`` and
    ``socks5h://``), but rejects the generic scheme before it can make a
    request. Treat the generic form as SOCKS5 so Rio can honor these proxy
    environment variables.
    """

    if proxy_url.lower().startswith("socks://"):
        return f"socks5://{proxy_url[len('socks://') :]}"
    return proxy_url


@contextmanager
def normalized_proxy_environment() -> Iterator[None]:
    """Temporarily normalize proxy environment variables for httpx construction."""

    original: dict[str, str | None] = {}
    changed = False
    for name in _PROXY_ENV_VARS:
        value = os.environ.get(name)
        if value is None:
            continue
        normalized = normalize_proxy_url(value)
        if normalized == value:
            continue
        original[name] = value
        os.environ[name] = normalized
        changed = True

    try:
        yield
    finally:
        if changed:
            for name, value in original.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def create_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """Create an ``httpx.AsyncClient`` with Rio's proxy normalization applied."""

    with normalized_proxy_environment():
        return httpx.AsyncClient(**kwargs)


class HTTPProvider[ConfigT: OpenAICompatibleConfig | AnthropicConfig | OpenAICodexConfig]:
    """Shared provider lifecycle: own the HTTP client and collect one final message.

    Subclasses name the ``api`` and ``provider`` recorded on the message and
    stream one request as provider events.
    """

    def __init__(self, config: ConfigT, *, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = client
        self._owns_client = client is None

    async def aclose(self) -> None:
        """Close the underlying HTTP client if this provider created it."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[UserMessage],
        tools: list[AgentTool],
    ) -> AssistantMessage:
        """Return one response as a final assistant message or error."""
        api, provider = self._labels()
        raw = self._stream_provider_events(
            model=model, system=system, messages=messages, tools=tools
        )
        return await collect_response(raw, api=api, provider=provider, model=model)

    def _labels(self) -> tuple[str, str]:
        raise NotImplementedError

    def _stream_provider_events(
        self,
        *,
        model: str,
        system: str,
        messages: list[UserMessage],
        tools: list[AgentTool],
    ) -> AsyncIterator[ProviderEvent]:
        raise NotImplementedError

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = create_async_client(timeout=self._config.timeout_seconds)
        return self._client


async def request_with_retries(
    client: httpx.AsyncClient,
    *,
    url: str,
    payload: Mapping[str, JSONValue],
    headers: Mapping[str, str] | Callable[[], Awaitable[Mapping[str, str]]],
    model: str,
    config: OpenAICompatibleConfig | AnthropicConfig | OpenAICodexConfig,
    parse: Callable[[httpx.Response, RetryAttempt], AsyncIterator[ProviderEvent]],
    is_retryable: Callable[[int, str], bool] | None = None,
) -> AsyncIterator[ProviderEvent]:
    """Own the streaming request, HTTP errors, and retry lifetime for every adapter."""

    async def attempt(state: RetryAttempt) -> AsyncIterator[ProviderEvent]:
        request_headers = await headers() if callable(headers) else headers
        async with client.stream("POST", url, json=payload, headers=request_headers) as response:
            header_name = getattr(config, "response_provider_header", None)
            response_provider = (
                response.headers.get(header_name, "").strip() or None if header_name else None
            )
            error = await check_response_status(
                response,
                lambda body: ProviderErrorEvent(
                    message=provider_http_error_message(
                        provider_name=config.provider_name,
                        status_code=response.status_code,
                        body=body,
                        model=model,
                    ),
                    data={
                        "status_code": response.status_code,
                        "body": body,
                        "attempts": state.number + 1,
                    },
                    response_provider=response_provider,
                ),
                is_retryable=is_retryable,
            )
            if error is not None:
                yield error
                return
            yield ProviderResponseStartEvent(model=model, response_provider=response_provider)
            async with aclosing(parse(response, state)) as events:
                async for event in events:
                    yield event

    async for event in stream_with_retries(
        attempt,
        max_retries=config.max_retries,
        max_retry_delay_seconds=config.max_retry_delay_seconds,
    ):
        yield event
