"""Inject one transport failure, then use the real provider through the public CLI."""

from __future__ import annotations

from typing import Any

from rio.ai import AssistantMessage
from rio.ai.provider import ClosableModelProvider
from rio.cli import main
from rio.cli import run as cli_run

create_provider = cli_run.create_model_provider


class TransientProvider:
    def __init__(self, provider: ClosableModelProvider) -> None:
        self.provider = provider
        self.failed = False

    async def complete(self, **kwargs: Any) -> AssistantMessage:
        if not self.failed:
            self.failed = True
            raise ConnectionError("Injected transient transport interruption")
        return await self.provider.complete(**kwargs)

    async def aclose(self) -> None:
        await self.provider.aclose()


def flaky_provider(*args: Any, **kwargs: Any) -> TransientProvider:
    return TransientProvider(create_provider(*args, **kwargs))


if __name__ == "__main__":
    cli_run.create_model_provider = flaky_provider
    main()
