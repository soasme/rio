"""Provider-neutral OAuth contracts and helpers shared by rio's login flows."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import secrets
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from os import environ
from typing import Any, Literal, Protocol
from urllib.parse import parse_qs, urlparse

import httpx

from rio.ai.env import RuntimeProviderAuth
from rio.ai.http import create_async_client
from rio.ai.types import JSONValue
from rio.coding.credentials import OAuthCredential

TOKEN_REFRESH_SKEW_MS = 60_000

OAuthFlowKind = Literal["browser", "device_code"]


@dataclass(frozen=True, slots=True)
class OAuthAuthInfo:
    """Authorization URL and optional instructions for a browser flow."""

    url: str
    instructions: str | None = None


@dataclass(frozen=True, slots=True)
class OAuthDeviceCodeInfo:
    """User-facing values returned by an OAuth device authorization request."""

    user_code: str
    verification_uri: str
    interval_seconds: float | None = None
    expires_in_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class OAuthPrompt:
    """Text input requested by an OAuth provider before or during login."""

    message: str
    placeholder: str | None = None


AuthCallback = Callable[[OAuthAuthInfo], None]
DeviceCodeCallback = Callable[[OAuthDeviceCodeInfo], None]
PromptCallback = Callable[[OAuthPrompt], Awaitable[str]]
ProgressCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class OAuthLoginCallbacks:
    """Frontend-independent callbacks available to an OAuth login flow."""

    on_auth: AuthCallback
    on_device_code: DeviceCodeCallback
    on_prompt: PromptCallback
    on_progress: ProgressCallback | None = None
    method: OAuthFlowKind | None = None


class OAuthProvider(Protocol):
    """Provider-specific OAuth behavior registered with rio."""

    @property
    def id(self) -> str:
        """Stable provider/credential identifier."""
        ...

    @property
    def name(self) -> str:
        """User-facing provider name."""
        ...

    async def login(self, callbacks: OAuthLoginCallbacks) -> OAuthCredential:
        """Complete login and return credentials to persist."""
        ...

    async def refresh(self, credential: OAuthCredential) -> OAuthCredential:
        """Refresh an expired credential."""
        ...

    def runtime_auth(self, credential: OAuthCredential) -> RuntimeProviderAuth:
        """Convert stored credentials to request auth."""
        ...


def oauth_metadata_string(
    metadata: Mapping[str, JSONValue],
    name: str,
) -> str | None:
    """Return one non-empty string from provider-specific OAuth metadata."""
    value = metadata.get(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


class OAuthError(RuntimeError):
    """Raised when an OAuth flow cannot complete."""


def oauth_credential_is_expired(credential: OAuthCredential) -> bool:
    """Return whether an OAuth credential should be refreshed before use."""
    return int(time.time() * 1000) >= credential.expires - TOKEN_REFRESH_SKEW_MS


@asynccontextmanager
async def oauth_client(
    client: httpx.AsyncClient | None, *, timeout: float = 30
) -> AsyncIterator[httpx.AsyncClient]:
    """Use ``client`` as given, or open and close a new one."""
    if client is not None:
        yield client
        return
    async with create_async_client(timeout=timeout) as owned:
        yield owned


def required_string(raw: Mapping[str, Any], name: str, label: str) -> str:
    """Return a non-empty string field from an OAuth response or raise."""
    value = raw.get(name)
    if not isinstance(value, str) or not value:
        raise OAuthError(f"{label} response missing {name}")
    return value


def optional_string(raw: Mapping[str, Any], name: str) -> str | None:
    """Return a non-empty string field from an OAuth response, if present."""
    value = raw.get(name)
    return value if isinstance(value, str) and value else None


def error_detail(response: httpx.Response, *, secrets: Iterable[str] = ()) -> str:
    """Summarize a token-endpoint failure body.

    The endpoint explains itself ("invalid_grant: Refresh token not found or
    invalid"); reporting only the status code turns a self-describing failure
    into a guessing game. Only the structured OAuth error fields are surfaced —
    the raw body stays out of the message because a failing request can echo
    the token it was sent. Those fields are server-controlled too, so anything
    we sent is scrubbed back out and the result is truncated before it reaches
    a log or the CLI.
    """
    try:
        raw = response.json()
    except ValueError:
        raw = None
    detail = _error_fields(raw) if isinstance(raw, dict) else None
    if detail is None:
        return "no error detail"
    for secret in secrets:
        if len(secret) >= 8:
            detail = detail.replace(secret, "<redacted>")
    return detail[:200]


def _error_fields(raw: dict[str, Any]) -> str | None:
    """Pull the error code and message out of either error envelope.

    OAuth failures arrive as flat ``error``/``error_description``, while the
    Anthropic API's own shape nests them under ``error`` as ``type``/``message``.
    """
    error = raw.get("error")
    description = raw.get("error_description") or raw.get("message")
    if isinstance(error, dict):
        description = description or error.get("message")
        error = error.get("type")
    if isinstance(error, str) and isinstance(description, str):
        return f"{error}: {description}"
    if isinstance(error, str):
        return error
    if isinstance(description, str):
        return description
    return None


def create_pkce_pair() -> tuple[str, str]:
    """Return a PKCE verifier and S256 challenge."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = _base64url(digest)
    return verifier, challenge


@dataclass(frozen=True, slots=True)
class AuthorizationCode:
    """Parsed OAuth authorization callback data."""

    code: str | None = None
    state: str | None = None


def parse_authorization_input(value: str) -> AuthorizationCode:
    """Parse a pasted redirect URL, query string, code#state pair, or raw code."""
    stripped = value.strip()
    if not stripped:
        return AuthorizationCode()

    parsed_url = urlparse(stripped)
    if parsed_url.scheme and parsed_url.netloc:
        params = parse_qs(parsed_url.query)
        return AuthorizationCode(
            code=_first_query_value(params, "code"),
            state=_first_query_value(params, "state"),
        )

    if "#" in stripped:
        code, state = stripped.split("#", 1)
        return AuthorizationCode(code=code or None, state=state or None)

    if "code=" in stripped:
        params = parse_qs(stripped)
        return AuthorizationCode(
            code=_first_query_value(params, "code"),
            state=_first_query_value(params, "state"),
        )

    return AuthorizationCode(code=stripped)


def validate_state(state: str | None, expected_state: str) -> None:
    if state is not None and state != expected_state:
        raise OAuthError("OAuth state mismatch")


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


class _LocalOAuthServer:
    def __init__(
        self,
        server: ThreadingHTTPServer,
        thread: threading.Thread,
        future: asyncio.Future[str | None],
    ) -> None:
        self._server = server
        self._thread = thread
        self._future = future

    async def wait_for_code(self) -> str | None:
        """Wait for the local callback server to receive a code."""
        return await self._future

    def close(self) -> None:
        """Stop the local callback server."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=1)


async def start_local_oauth_server(
    state: str,
    *,
    callback_port: int,
    callback_path: str,
    success_message: str,
) -> _LocalOAuthServer | None:
    host = environ.get("RIO_OAUTH_CALLBACK_HOST", "127.0.0.1")
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str | None] = loop.create_future()

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            try:
                parsed = urlparse(self.path)
                if parsed.path != callback_path:
                    self._finish(404, _oauth_html("Callback route not found."))
                    return
                params = parse_qs(parsed.query)
                if _first_query_value(params, "state") != state:
                    self._finish(400, _oauth_html("OAuth state mismatch."))
                    return
                code = _first_query_value(params, "code")
                if not code:
                    self._finish(400, _oauth_html("Missing authorization code."))
                    return
                self._finish(
                    200,
                    _oauth_html(success_message),
                )
                if not future.done():
                    loop.call_soon_threadsafe(future.set_result, code)
            except Exception:
                self._finish(500, _oauth_html("Internal error while processing OAuth callback."))

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _finish(self, status: int, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    try:
        server = ThreadingHTTPServer((host, callback_port), CallbackHandler)
    except OSError:
        return None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return _LocalOAuthServer(server, thread, future)


def _first_query_value(params: dict[str, list[str]], key: str) -> str | None:
    values = params.get(key)
    if not values:
        return None
    return values[0] or None


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _oauth_html(message: str) -> str:
    escaped = (
        message.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    return f'<!doctype html><meta charset="utf-8"><title>rio OAuth</title><p>{escaped}</p>'
