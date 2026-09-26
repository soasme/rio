"""Append-only history and state-rebuild behavior of the agent runtime."""

from __future__ import annotations

import pytest

from conftest import make_skill, step_response
from rio.agent import run_skill_loop
from rio.ai import AgentTool, AgentToolResult, FakeProvider, TextContent


async def _long_result(tool_call_id, arguments, signal=None, on_update=None):
    return AgentToolResult(content=[TextContent(text="evidence " * 80)])


LONG_RESULT = AgentTool(
    name="advance",
    label="Advance",
    description="Produce evidence.",
    parameters={"type": "object", "properties": {}},
    execute_fn=_long_result,
)


@pytest.mark.asyncio
async def test_history_appends_state_patches_and_observations_without_reasoning():
    provider = FakeProvider(
        [
            step_response(
                reasoning="PRIVATE", state_delta={"counter": 1}, action="advance", args={}
            ),
            step_response(reasoning="PRIVATE_TOO", state_delta={}, action="finish", args={}),
        ]
    )

    async for _ in run_skill_loop(
        provider=provider, model="m", skill=make_skill(), observation="do the task"
    ):
        pass

    second_history = provider.calls[1][2]
    contents = [message.content for message in second_history]
    assert any("Initial state:" in content for content in contents)
    assert any("Observation:\ndo the task" in content for content in contents)
    assert any('State patch:\n```json\n{\n  "counter": 1' in content for content in contents)
    assert any("Observation:\nobserved:" in content for content in contents)
    assert "PRIVATE" not in "\n".join(contents)


@pytest.mark.asyncio
async def test_history_is_rebuilt_to_one_materialized_state_at_eighty_percent():
    provider = FakeProvider(
        [
            step_response(reasoning="", state_delta={"counter": 1}, action="advance", args={}),
            step_response(reasoning="", state_delta={"counter": 2}, action="advance", args={}),
            step_response(reasoning="", state_delta={}, action="finish", args={}),
        ]
    )

    async for _ in run_skill_loop(
        provider=provider,
        model="m",
        skill=make_skill(actions=(LONG_RESULT, make_skill().actions[1])),
        observation="do the task",
        context_window_tokens=300,
    ):
        pass

    rebuilt_history = provider.calls[2][2]
    assert len(rebuilt_history) == 1
    assert rebuilt_history[0].content.startswith("State rebuild:")
    assert '"counter": 2' in rebuilt_history[0].content
