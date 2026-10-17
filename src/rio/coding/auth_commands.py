"""Terminal commands for provider authentication."""

from getpass import getpass

import anyio

from rio.coding.credentials import FileCredentialStore
from rio.coding.oauth import OAuthFlowKind, OAuthLoginCallbacks, OAuthPrompt
from rio.coding.oauth_registry import get_oauth_provider
from rio.coding.provider_config import (
    ProviderConfig,
    ProviderConfigError,
    load_provider_settings,
    provider_config_from_catalog_entry,
    save_default_provider,
)


def _provider(name: str) -> ProviderConfig:
    try:
        return load_provider_settings().get_provider(name)
    except ProviderConfigError:
        return provider_config_from_catalog_entry(name)


def terminal_callbacks(method: OAuthFlowKind | None = None) -> OAuthLoginCallbacks:
    async def prompt(value: OAuthPrompt) -> str:
        return await anyio.to_thread.run_sync(lambda: input(f"{value.message}: "))

    return OAuthLoginCallbacks(
        on_auth=lambda info: print(f"{info.url}\n{info.instructions or ''}"),
        on_device_code=lambda info: print(f"{info.verification_uri}\nCode: {info.user_code}"),
        on_prompt=prompt,
        on_progress=print,
        method=method,
    )


async def login_provider(name: str, *, method: str | None = None) -> str:
    """Authenticate a provider and persist credentials without displaying secrets."""
    provider = _provider(name)
    key = provider.credential_name or provider.name
    store = FileCredentialStore()
    oauth = get_oauth_provider(name)
    if method not in (None, "browser", "device_code", "api-key"):
        raise ValueError("Login method must be browser, device_code, or api-key")
    if oauth is not None and method != "api-key":
        credential = await oauth.login(
            terminal_callbacks(
                "browser"
                if method == "browser"
                else "device_code"
                if method == "device_code"
                else None
            )
        )
        store.set_oauth(key, credential)
    else:
        secret = await anyio.to_thread.run_sync(lambda: getpass(f"API key for {name}: "))
        if not secret.strip():
            raise ValueError("API key cannot be empty")
        store.set(key, secret)
    save_default_provider(provider.name)
    return f"Logged in to {name}."
