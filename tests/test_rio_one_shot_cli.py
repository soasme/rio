"""One-shot CLI checks."""

import pytest

from conftest import make_skill, step_response
from rio.agent import run_notebook_loop
from rio.ai import FakeProvider
from rio.cli import app


def test_cli_requires_a_task():
    with pytest.raises(SystemExit) as error:
        app(["run"])
    assert error.value.code == 2


@pytest.mark.asyncio
async def test_task_is_the_first_user_cell():
    provider = FakeProvider([step_response(reply="done")])

    async for _ in run_notebook_loop(
        provider=provider, model="fake", skill=make_skill(), observation="fix the parser"
    ):
        pass

    assert '"source": "fix the parser"' in provider.calls[0][2][0].text
