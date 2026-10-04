"""Tests for Harness, the stateful wrapper around run_notebook_loop."""

from __future__ import annotations

import pytest

from conftest import make_skill, step_response
from rio.coding.agent import Harness, HarnessConfig, RunEndEvent, markdown_cell, new_notebook


@pytest.mark.asyncio
async def test_harness_tracks_the_notebook_and_notifies_listeners():
    from rio.ai import FakeProvider

    provider = FakeProvider([step_response(reply="r1")])
    harness = Harness(HarnessConfig(provider=provider, model="m", skill=make_skill()))
    received = []
    harness.subscribe(received.append)

    events = [event async for event in harness.run("start")]

    assert [cell["source"] for cell in harness.notebook["cells"]] == ["start", "r1"]
    assert not harness.is_running
    assert received == events
    assert isinstance(events[-1], RunEndEvent)


@pytest.mark.asyncio
async def test_harness_rejects_concurrent_run():
    from rio.ai import FakeProvider

    harness = Harness(
        HarnessConfig(
            provider=FakeProvider([step_response(reply="r")]), model="m", skill=make_skill()
        )
    )

    generator = harness.run("start")
    with pytest.raises(RuntimeError):
        harness.run("start-again")
    async for _ in generator:
        pass

    assert not harness.is_running


@pytest.mark.asyncio
async def test_harness_continues_from_an_explicit_notebook():
    from rio.ai import FakeProvider

    provider = FakeProvider([step_response(reply="r")])
    notebook = new_notebook()
    notebook["cells"].append(markdown_cell("earlier work"))
    harness = Harness(
        HarnessConfig(provider=provider, model="m", skill=make_skill()), notebook=notebook
    )

    async for _ in harness.run("next task"):
        pass

    request = provider.calls[0][2][0].text
    assert "earlier work" in request and "next task" in request
    assert notebook["cells"] == [harness.notebook["cells"][0]]
