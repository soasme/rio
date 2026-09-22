"""Provider authentication command."""

from __future__ import annotations

from typing import Annotated

import anyio
import typer

from rio.coding.auth_commands import login_provider


def login(
    provider: Annotated[str, typer.Argument(help="Provider name.")],
    method: Annotated[
        str | None, typer.Option("--method", help="Authentication method.")
    ] = None,
) -> None:
    """Log in to a provider and save its credentials."""
    try:
        message = anyio.run(_login, provider, method)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--method") from exc
    typer.echo(message)


async def _login(provider: str, method: str | None) -> str:
    return await login_provider(provider, method=method)
