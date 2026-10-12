"""Run with ``uv run python src/rio/coding/data/examples/offline_session.py``."""

import asyncio
import tempfile
from pathlib import Path

from rio.agent.runner import run
from rio.agent.session import Session
from rio.agent.store import Store
from rio.ai import AssistantDoneEvent, AssistantMessage, FakeProvider, ToolCall


async def main() -> None:
    message = AssistantMessage(
        content=[
            ToolCall(
                id="example-step",
                name="step",
                arguments={
                    "ops": [
                        {
                            "op": "add",
                            "cell": {
                                "kind": "note",
                                "role": "conclusion",
                                "text": "Hello from Rio.",
                                "result": "success",
                            },
                        }
                    ]
                },
            )
        ],
        stop_reason="toolUse",
    )
    provider = FakeProvider([[AssistantDoneEvent(reason="toolUse", message=message)]])
    with (
        tempfile.TemporaryDirectory() as directory,
        Store(Path(directory) / "session.sqlite3") as store,
    ):
        session = Session.create(store, "Say hello", Path.cwd())
        assert await run(session, provider, "offline", print)


if __name__ == "__main__":
    asyncio.run(main())
