"""One-shot CLI and state-only prompt checks."""

import pytest
from typer.testing import CliRunner

from conftest import make_skill, step_response
from rio.agent import run_skill_loop
from rio.ai import FakeProvider
from rio.cli import app


def test_cli_requires_a_task():
    assert CliRunner().invoke(app, []).exit_code == 2


@pytest.mark.asyncio
async def test_task_is_not_an_initial_observation():
    provider = FakeProvider(
        [step_response(reasoning="done", state_delta={}, action="finish", args={})]
    )
    skill = make_skill()
    state = dict(skill.initial_state)
    state["goal"] = "fix the parser"

    async for _ in run_skill_loop(provider=provider, model="fake", skill=skill, state=state):
        pass

    prompt = provider.calls[0][2][0].text
    assert "fix the parser" in prompt
    assert "Previous Action Result" not in prompt
