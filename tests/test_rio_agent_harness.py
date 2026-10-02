"""Tests for Harness, the stateful wrapper around run_context_loop."""

from __future__ import annotations

import pytest

from conftest import make_skill, step_response
from rio.agent import Harness, HarnessConfig, RunEndEvent, turn
from rio.ai import FakeProvider


@pytest.mark.asyncio
async def test_harness_tracks_context_and_notifies_listeners():
    provider = FakeProvider([step_response(reasoning="r1", action="finish")])
    harness = Harness(HarnessConfig(provider=provider, model="m", skill=make_skill()))
    received = []
    harness.subscribe(received.append)

    events = [event async for event in harness.run("start")]

    assert [item["role"] for item in harness.context] == ["user", "assistant", "tool"]
    assert not harness.is_running
    assert received == events
    assert isinstance(events[-1], RunEndEvent)


@pytest.mark.asyncio
async def test_harness_rejects_concurrent_run():
    provider = FakeProvider([step_response(reasoning="r1", action="finish")])
    harness = Harness(HarnessConfig(provider=provider, model="m", skill=make_skill()))

    generator = harness.run("start")
    with pytest.raises(RuntimeError):
        harness.run("start-again")

    async for _ in generator:
        pass

    assert not harness.is_running


@pytest.mark.asyncio
async def test_harness_continues_from_an_explicit_context():
    provider = FakeProvider([step_response(reasoning="r1", action="finish")])
    harness = Harness(
        HarnessConfig(provider=provider, model="m", skill=make_skill()),
        context=[turn("notes", "earlier work")],
    )

    async for _ in harness.run("next task"):
        pass

    first_request = provider.calls[0][2]
    assert "earlier work" in first_request[0].text
    assert "next task" in first_request[0].text
    assert harness.context[0] == turn("notes", "earlier work")


@pytest.mark.asyncio
async def test_harness_keeps_one_context_file_across_runs():
    provider = FakeProvider([step_response(action="finish"), step_response(action="finish")])
    harness = Harness(HarnessConfig(provider=provider, model="m", skill=make_skill()))

    async for _ in harness.run("one"):
        pass
    async for _ in harness.run("two"):
        pass

    path = str(harness.context_file)
    assert path in provider.calls[0][1]
    assert path in provider.calls[1][1]
