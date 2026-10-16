"""Run with ``uv run python src/rio/coding/data/examples/offline_session.py``."""

import asyncio
import tempfile
from pathlib import Path

from rio.agent.runner import Owner, run
from rio.agent.session import Session
from rio.agent.store import Store
from rio.ai import AssistantMessage, FakeProvider, ToolCall


async def main() -> None:
    message = AssistantMessage(
        content=[
            ToolCall(
                id="example-step",
                name="step",
                arguments={
                    "ops": [
                        {
                            "op": "control",
                            "control": {
                                "type": "conclusion",
                                "result": "success",
                                "reason": "Hello from Rio.",
                            },
                        }
                    ]
                },
            )
        ],
        stop_reason="toolUse",
    )
    provider = FakeProvider([message])
    with (
        tempfile.TemporaryDirectory() as directory,
        Store(Path(directory) / "session.sqlite3") as store,
    ):
        session = Session.create(store, "Say hello")
        assert await run(Owner(session), provider, "offline", print)


if __name__ == "__main__":
    asyncio.run(main())
