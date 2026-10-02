"""One-shot CLI checks."""

import pytest

from conftest import make_skill, step_response
from rio.agent import run_context_loop
from rio.ai import FakeProvider
from rio.cli import app


def test_cli_requires_a_task():
    with pytest.raises(SystemExit) as error:
        app(["run"])
    assert error.value.code == 2


@pytest.mark.asyncio
async def test_task_is_the_first_user_turn():
    provider = FakeProvider([step_response(reasoning="done", action="finish", args={})])

    async for _ in run_context_loop(
        provider=provider, model="fake", skill=make_skill(), observation="fix the parser"
    ):
        pass

    assert provider.calls[0][2][0].text == "fix the parser"
