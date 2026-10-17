"""Runtime provider construction for rio coding sessions."""

from __future__ import annotations

import asyncio
from asyncio import AbstractEventLoop, get_running_loop
from collections.abc import MutableMapping
from dataclasses import replace
from os import environ
from weakref import WeakKeyDictionary

from rio.ai.anthropic import AnthropicProvider
from rio.ai.env import RuntimeProviderAuth
from rio.ai.google import GoogleGenerativeAIProvider
from rio.ai.mistral import MistralConversationsProvider
from rio.ai.openai_codex import (
    OpenAICodexConfig,
    OpenAICodexCredentials,
    OpenAICodexProvider,
)
from rio.ai.openai_compatible import OpenAICompatibleProvider
from rio.ai.provider import ClosableModelProvider
from rio.coding.credentials import FileCredentialStore, OAuthCredential
from rio.coding.oauth import OAuthProvider, oauth_credential_is_expired
from rio.coding.oauth_openai_codex import account_id_from_access_token
from rio.coding.oauth_registry import get_oauth_provider
from rio.coding.provider_config import (
    AGENT_TRANSPORT_RETRIES,
    ProviderConfig,
    anthropic_cache_settings,
    anthropic_config_from_provider,
    google_config_from_provider,
    mistral_config_from_provider,
    openai_compatible_config_from_provider,
    reasoning_effort_from_provider,
    resolve_model_endpoint,
    validate_provider_model,
)
from rio.coding.thinking import ThinkingLevel

# Subscription OAuth that requires a fixed system prompt ahead of Rio's own.
_OAUTH_SYSTEM_PROMPTS = {
    "anthropic": "You are Claude Code, Anthropic's official CLI for Claude.",
}


def create_model_provider(
    provider: ProviderConfig,
    *,
    credential_store: FileCredentialStore | None = None,
    model: str | None = None,
    thinking_level: ThinkingLevel | None = None,
) -> ClosableModelProvider:
    """Create a runtime model provider from durable provider settings."""
    if model is not None:
        validate_provider_model(provider, model)
    credentials = credential_store or FileCredentialStore()
    # Resolve model/provider precedence once; every branch reads the result.
    endpoint = resolve_model_endpoint(provider, model)
    api = endpoint.api
    if api == "openai-codex-responses":
        return OpenAICodexProvider(
            OpenAICodexConfig(
                credential_resolver=OpenAICodexCredentialResolver(
                    provider,
                    credential_store=credentials,
                ),
                base_url=endpoint.base_url,
                provider_name=provider.name,
                headers=endpoint.headers,
                timeout_seconds=provider.timeout_seconds,
                max_retries=AGENT_TRANSPORT_RETRIES,
                reasoning_effort=reasoning_effort_from_provider(
                    provider,
                    model=model,
                    thinking_level=thinking_level,
                ),
            )
        )
    builder_args = {
        "credential_reader": credentials,
        "model": model,
        "thinking_level": thinking_level,
    }
    if api == "google-generative-ai":
        return GoogleGenerativeAIProvider(google_config_from_provider(provider, **builder_args))
    if api == "mistral-conversations":
        return MistralConversationsProvider(mistral_config_from_provider(provider, **builder_args))
    credential = _oauth_credential(provider, credentials)
    runtime_auth = (
        _required_oauth_provider(provider.name).runtime_auth(credential)
        if credential is not None
        else None
    )
    if api == "anthropic-messages":
        config = anthropic_config_from_provider(provider, **builder_args)
        if runtime_auth is not None:
            oauth_retention, _ = anthropic_cache_settings(provider, model, oauth=True)
            config = replace(
                config,
                api_key=runtime_auth.api_key,
                bearer_auth=True,
                base_url=runtime_auth.base_url or config.base_url,
                headers={**dict(config.headers or {}), **dict(runtime_auth.headers or {})},
                oauth_system_prompt=_OAUTH_SYSTEM_PROMPTS.get(provider.name),
                cache_retention=oauth_retention,
                credential_resolver=OAuthRuntimeCredentialResolver(
                    provider,
                    credential_store=credentials,
                ),
            )
        return AnthropicProvider(config)
    compatible_config = openai_compatible_config_from_provider(provider, **builder_args)
    if runtime_auth is not None:
        compatible_config = replace(
            compatible_config,
            api_key=runtime_auth.api_key,
            base_url=runtime_auth.base_url or compatible_config.base_url,
            headers={
                **dict(compatible_config.headers or {}),
                **dict(runtime_auth.headers or {}),
            },
            credential_resolver=OAuthRuntimeCredentialResolver(
                provider,
                credential_store=credentials,
            ),
        )
    return OpenAICompatibleProvider(compatible_config)


class OpenAICodexCredentialResolver:
    """Resolve and refresh OpenAI Codex OAuth credentials for one request."""

    def __init__(
        self,
        provider: ProviderConfig,
        *,
        credential_store: FileCredentialStore,
    ) -> None:
        self._provider = provider
        self._credential_store = credential_store

    async def __call__(self) -> OpenAICodexCredentials:
        """Return a valid Codex access token and account id."""
        credential_name = self._provider.credential_name
        if credential_name:
            credential = self._credential_store.get_oauth(credential_name)
            if credential is not None:
                credential = await OAuthRuntimeCredentialResolver(
                    self._provider, credential_store=self._credential_store
                ).credential()
                if credential.account_id is None:
                    raise RuntimeError("OpenAI Codex OAuth credential is missing account_id")
                return OpenAICodexCredentials(
                    access_token=credential.access,
                    account_id=credential.account_id,
                )

        access_token = environ.get(self._provider.api_key_env)
        if access_token:
            account_id = account_id_from_access_token(access_token)
            if account_id is None:
                raise RuntimeError(
                    f"{self._provider.api_key_env} must contain an OpenAI Codex access JWT"
                )
            return OpenAICodexCredentials(access_token=access_token, account_id=account_id)

        credential_hint = f"Run /login {self._provider.name}."
        raise RuntimeError(f"Missing OpenAI Codex OAuth credentials. {credential_hint}")


_REFRESH_LOCKS: MutableMapping[AbstractEventLoop, dict[str, asyncio.Lock]] = WeakKeyDictionary()


def _refresh_lock(credential_name: str) -> asyncio.Lock:
    """Return this loop's refresh lock for one stored credential.

    Providers rotate the refresh token on use: the old one dies the moment a
    refresh succeeds. A session issues provider calls concurrently (the agent
    loop and session auto-naming, for two), so without serialization several
    tasks read the same expired credential and spend the same refresh token.
    One of them wins, the losers 400, and whichever write lands last can leave
    a superseded token on disk — which fails on the *next* run, long after the
    race that caused it. Holding this lock across the network call, and
    re-reading the store inside it, keeps a token spent at most once.

    Locks are cached per event loop because ``asyncio.Lock`` binds to the
    running loop on first contention: a lock cached across loops appears to
    work — the uncontended path never touches the loop — until two tasks
    contend it in a later loop and it raises.
    """
    locks = _REFRESH_LOCKS.setdefault(get_running_loop(), {})
    lock = locks.get(credential_name)
    if lock is None:
        lock = asyncio.Lock()
        locks[credential_name] = lock
    return lock


def _oauth_credential(
    provider: ProviderConfig,
    credential_store: FileCredentialStore,
) -> OAuthCredential | None:
    if provider.credential_name is None or get_oauth_provider(provider.name) is None:
        return None
    return credential_store.get_oauth(provider.credential_name)


class OAuthRuntimeCredentialResolver:
    """Refresh provider-neutral OAuth credentials immediately before a request."""

    def __init__(
        self,
        provider: ProviderConfig,
        *,
        credential_store: FileCredentialStore,
    ) -> None:
        self._provider = provider
        self._credential_store = credential_store

    async def credential(self) -> OAuthCredential:
        """Read and refresh stored credentials under the shared rotation lock."""
        credential_name = self._provider.credential_name
        if credential_name is None:
            raise RuntimeError(f"Provider {self._provider.name} has no credential name")
        oauth_provider = _required_oauth_provider(self._provider.name)
        async with _refresh_lock(credential_name), self._credential_store.refresh_lock():
            # Read inside the lock: a task that waited here while another
            # refreshed sees the rotated credential and skips its own refresh.
            credential = self._credential_store.get_oauth(credential_name)
            if credential is None:
                raise RuntimeError(
                    f"Missing OAuth credentials for {self._provider.name}. "
                    f"Run /login {self._provider.name}."
                )
            if oauth_credential_is_expired(credential):
                credential = await oauth_provider.refresh(credential)
                self._credential_store.set_oauth(credential_name, credential)
        return credential

    async def __call__(self) -> RuntimeProviderAuth:
        credential = await self.credential()
        return _required_oauth_provider(self._provider.name).runtime_auth(credential)


def _required_oauth_provider(provider_name: str) -> OAuthProvider:
    oauth_provider = get_oauth_provider(provider_name)
    if oauth_provider is None:
        raise RuntimeError(f"No OAuth implementation is registered for {provider_name}")
    return oauth_provider
