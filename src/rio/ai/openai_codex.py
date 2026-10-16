"""OpenAI Codex subscription Responses provider."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from platform import machine, release, system

import httpx

from rio.ai._provider_events import (
    ProviderErrorEvent,
    ProviderEvent,
    ProviderTextDeltaEvent,
    ProviderThinkingDeltaEvent,
    ProviderToolCallEvent,
)
from rio.ai.env import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_RETRY_DELAY_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
)
from rio.ai.http import HTTPProvider, request_with_retries
from rio.ai.messages import UserMessage
from rio.ai.openai_compatible import _ResponsesStreamParser, _stream_error_details
from rio.ai.retry import (
    RetryableFailure,
    RetryAttempt,
    is_transient_status,
)
from rio.ai.tools import AgentTool
from rio.ai.types import JSONValue

DEFAULT_OPENAI_CODEX_BASE_URL = "https://chatgpt.com/backend-api"


@dataclass(frozen=True, slots=True)
class OpenAICodexCredentials:
    """Bearer token and account id required by ChatGPT Codex Responses."""

    access_token: str
    account_id: str


type OpenAICodexCredentialResolver = Callable[[], Awaitable[OpenAICodexCredentials]]


@dataclass(frozen=True, slots=True)
class OpenAICodexConfig:
    """Configuration for the OpenAI Codex subscription Responses endpoint."""

    credential_resolver: OpenAICodexCredentialResolver
    base_url: str = DEFAULT_OPENAI_CODEX_BASE_URL
    headers: Mapping[str, str] | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    max_retry_delay_seconds: float = DEFAULT_MAX_RETRY_DELAY_SECONDS
    originator: str = "tau"
    reasoning_effort: str | None = None
    provider_name: str = "OpenAI Codex"


class OpenAICodexProvider(HTTPProvider[OpenAICodexConfig]):
    """Provider adapter for ChatGPT subscription Codex Responses over SSE."""

    def _labels(self) -> tuple[str, str]:
        return "openai-codex-responses", "openai-codex"

    def _stream_provider_events(
        self,
        *,
        model: str,
        system: str,
        messages: list[UserMessage],
        tools: list[AgentTool],
    ) -> AsyncIterator[ProviderEvent]:
        """Stream one Codex Responses request as provider-neutral events."""

        async def iterator() -> AsyncIterator[ProviderEvent]:
            client = self._get_client()
            payload = _build_codex_payload(
                model=model,
                system=system,
                messages=messages,
                tools=tools,
                reasoning_effort=self._config.reasoning_effort,
            )
            url = _resolve_codex_url(self._config.base_url)

            async def headers() -> Mapping[str, str]:
                credentials = await self._config.credential_resolver()
                return _build_codex_headers(
                    self._config.headers,
                    access_token=credentials.access_token,
                    account_id=credentials.account_id,
                    originator=self._config.originator,
                )

            async def parse(
                response: httpx.Response, attempt: RetryAttempt
            ) -> AsyncIterator[ProviderEvent]:
                # Thinking alone still allows a network retry, not a stream-error retry.
                emitted_thinking = False
                async for event in _codex_provider_events(response):
                    if isinstance(event, ProviderTextDeltaEvent | ProviderToolCallEvent):
                        attempt.emitted_content = True
                    elif isinstance(event, ProviderThinkingDeltaEvent):
                        emitted_thinking = True
                    if (
                        isinstance(event, ProviderErrorEvent)
                        and not emitted_thinking
                        and _retryable_stream_error_event(event)
                    ):
                        raw = _stream_error_event_data(event) or {}
                        code, _message = _stream_error_details(raw)
                        raise RetryableFailure(
                            f"stream error ({code or 'unknown'})",
                            event,
                            data={"event": raw},
                        )
                    yield event

            try:
                async for event in request_with_retries(
                    client,
                    url=url,
                    payload=payload,
                    headers=headers,
                    model=model,
                    config=self._config,
                    parse=parse,
                    is_retryable=_is_retryable_status,
                ):
                    yield event
            except Exception as exc:
                yield ProviderErrorEvent(message=str(exc))

        return iterator()


def _build_codex_payload(
    *,
    model: str,
    system: str,
    messages: list[UserMessage],
    tools: list[AgentTool],
    reasoning_effort: str | None = None,
) -> dict[str, JSONValue]:
    payload: dict[str, JSONValue] = {
        "model": model,
        "store": False,
        "stream": True,
        "instructions": system or "You are a helpful assistant.",
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": message.content}]}
            for message in messages
        ],
        "text": {"verbosity": "low"},
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }
    if reasoning_effort is not None:
        payload["reasoning"] = {
            "effort": reasoning_effort,
            "summary": "auto",
        }
    if tools:
        payload["tools"] = [_tool_to_codex(tool) for tool in tools]
    return payload


def _tool_to_codex(tool: AgentTool) -> dict[str, JSONValue]:
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": dict(tool.parameters),
        "strict": False,
    }


async def _codex_provider_events(response: httpx.Response) -> AsyncIterator[ProviderEvent]:
    parser = _ResponsesStreamParser(codex=True)
    async for chunk in _iter_sse_data(response):
        events, stop = parser.feed(chunk)
        for event in events:
            yield event
        if stop:
            break
    if not parser.fatal:
        for event in parser.finalize():
            yield event


async def _iter_sse_data(response: httpx.Response) -> AsyncIterator[str]:
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        stripped = line.strip()
        if not stripped:
            if data_lines:
                data = "\n".join(data_lines).strip()
                data_lines = []
                yield data
            continue
        if not stripped.startswith("data:"):
            continue
        value = stripped.removeprefix("data:").strip()
        if value == "[DONE]":
            break
        data_lines.append(value)

    if data_lines:
        yield "\n".join(data_lines).strip()


_TRANSIENT_STREAM_ERROR_MARKERS = (
    "overloaded",
    "model is at capacity",
    "currently experiencing high demand",
    "service_unavailable",
    "temporarily_unavailable",
    "rate_limit",
    "internal_error",
    "server_error",
    "timeout",
)


def _stream_error_event_data(event: ProviderErrorEvent) -> dict[str, JSONValue] | None:
    """Return the raw SSE event attached to a provider stream error."""
    data = event.data
    if not isinstance(data, dict):
        return None
    raw = data.get("event")
    return raw if isinstance(raw, dict) else None


def _retryable_stream_error_event(event: ProviderErrorEvent) -> bool:
    """Return True when an in-stream Codex error looks transient and retryable."""
    raw = _stream_error_event_data(event)
    if raw is None:
        return False
    code, message = _stream_error_details(raw)
    haystack = " ".join(part for part in (code, message) if part).lower()
    if not haystack or _is_terminal_rate_limit(haystack):
        return False
    return any(marker in haystack for marker in _TRANSIENT_STREAM_ERROR_MARKERS)


def _build_codex_headers(
    configured_headers: Mapping[str, str] | None,
    *,
    access_token: str,
    account_id: str,
    originator: str,
) -> dict[str, str]:
    headers = {
        **dict(configured_headers or {}),
        "Authorization": f"Bearer {access_token}",
        "chatgpt-account-id": account_id,
        "originator": originator,
        "User-Agent": f"tau ({system()} {release()}; {machine()})",
        "OpenAI-Beta": "responses=experimental",
        "accept": "text/event-stream",
        "content-type": "application/json",
    }
    return headers


def _resolve_codex_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/codex/responses"):
        return normalized
    if normalized.endswith("/codex"):
        return f"{normalized}/responses"
    return f"{normalized}/codex/responses"


def _is_retryable_status(status_code: int, body: str) -> bool:
    if status_code == 429 and _is_terminal_rate_limit(body):
        return False
    return is_transient_status(status_code)


def _is_terminal_rate_limit(body: str) -> bool:
    normalized = body.lower()
    markers = (
        "gousagelimiterror",
        "freeusagelimiterror",
        "monthly usage limit reached",
        "available balance",
        "insufficient_quota",
        "out of budget",
        "quota exceeded",
        "billing",
        "subscription_sharing_usage_limit_exceeded",
    )
    return any(marker in normalized for marker in markers)
