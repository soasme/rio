"""Provider-neutral OAuth contracts used by rio's coding application."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from rio.ai.types import JSONValue
from rio.coding.credentials import OAuthCredential

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


@dataclass(frozen=True, slots=True)
class OAuthRuntimeAuth:
    """Request authentication derived from a stored OAuth credential."""

    api_key: str
    base_url: str | None = None
    headers: Mapping[str, str] | None = None


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

    def runtime_auth(self, credential: OAuthCredential) -> OAuthRuntimeAuth:
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
