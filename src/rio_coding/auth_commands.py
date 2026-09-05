"""Credential commands with injectable interaction for terminal frontends."""

from dataclasses import replace

import anyio
import typer

from rio_coding.credentials import FileCredentialStore
from rio_coding.oauth_registry import get_oauth_provider
from rio_coding.oauth_types import OAuthLoginCallbacks
from rio_coding.provider_config import (
    ProviderConfigError,
    load_provider_settings,
    provider_config_from_catalog_entry,
    upsert_saved_provider,
)


def _provider(name):
    try:
        return load_provider_settings().get_provider(name)
    except ProviderConfigError:
        return provider_config_from_catalog_entry(name)


def terminal_callbacks(method=None):
    async def prompt(value):
        return await anyio.to_thread.run_sync(lambda: typer.prompt(value.message))

    async def select(value):
        choices = ", ".join(f"{item.id}: {item.label}" for item in value.options)
        return await anyio.to_thread.run_sync(lambda: typer.prompt(f"{value.message} ({choices})"))

    return OAuthLoginCallbacks(
        on_auth=lambda info: typer.echo(f"{info.url}\n{info.instructions or ''}"),
        on_device_code=lambda info: typer.echo(f"{info.verification_uri}\nCode: {info.user_code}"),
        on_prompt=prompt,
        on_select=select,
        on_progress=typer.echo,
        method=method,
    )


async def login_provider(name, *, method=None, callbacks=None, api_key=None):
    """Authenticate a provider and persist credentials without displaying secrets."""
    provider = _provider(name)
    key = provider.credential_name or provider.name
    store = FileCredentialStore()
    oauth = get_oauth_provider(name)
    if method not in (None, "browser", "device_code", "api-key"):
        raise ValueError("Login method must be browser, device_code, or api-key")
    if oauth is not None and method != "api-key" and api_key is None:
        credential = await oauth.login(callbacks or terminal_callbacks(method))
        store.set_oauth(key, credential)
    else:
        secret = api_key
        if secret is None:
            secret = await anyio.to_thread.run_sync(
                lambda: typer.prompt(f"API key for {name}", hide_input=True)
            )
        if not secret.strip():
            raise ValueError("API key cannot be empty")
        store.set_api_key(key, secret)
    upsert_saved_provider(replace(provider, credential_name=key))
    return f"Logged in to {name}."


def logout_provider(name):
    """Remove only this provider's stored credentials."""
    provider = _provider(name)
    FileCredentialStore().delete(provider.credential_name or provider.name)
    return f"Logged out of {name}."
