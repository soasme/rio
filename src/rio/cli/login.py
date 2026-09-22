"""Provider authentication command."""

from __future__ import annotations

import anyio

from rio.coding.auth_commands import login_provider


def login(provider: str, method: str | None = None) -> str:
    """Log in to a provider and save its credentials."""
    return anyio.run(_login, provider, method)


async def _login(provider: str, method: str | None) -> str:
    return await login_provider(provider, method=method)
