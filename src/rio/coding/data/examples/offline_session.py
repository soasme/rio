"""Run with ``uv run python src/rio/coding/data/examples/offline_session.py``."""

import asyncio
from pathlib import Path

from rio.ai import AssistantDoneEvent, AssistantMessage, FakeProvider, ToolCall
from rio.coding import CodingSession, CodingSessionConfig
from rio.coding.rendering import PlainEventRenderer
from rio.coding.session_store import InMemorySessionStorage


async def main() -> None:
    message = AssistantMessage(
        content=[
            ToolCall(
                id="example-step",
                name="skill_step",
                arguments={
                    "state_delta": {"goal": "greet the user"},
                    "action": {"name": "respond", "arguments": {"message": "Hello from Rio."}},
                },
            )
        ],
        stop_reason="toolUse",
    )
    provider = FakeProvider([[AssistantDoneEvent(reason="toolUse", message=message)]])
    session = await CodingSession.load(
        CodingSessionConfig(
            provider=provider,
            model="offline",
            cwd=Path.cwd(),
            storage=InMemorySessionStorage(),
            project_resources_trusted=False,
            load_extensions=False,
        )
    )
    renderer = PlainEventRenderer()
    try:
        async for event in session.prompt("Say hello"):
            renderer.render(event)
    finally:
        await session.aclose()
    assert session.answer == "Hello from Rio."


if __name__ == "__main__":
    asyncio.run(main())
