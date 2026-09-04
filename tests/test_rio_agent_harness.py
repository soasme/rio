"""Tests for SkillStateHarness, the stateful wrapper around run_skill_loop."""

from __future__ import annotations

import pytest

from conftest import make_skill, step_response
from rio_agent import RunEndEvent, SkillStateHarness, SkillStateHarnessConfig
from rio_ai import FakeProvider


@pytest.mark.asyncio
async def test_harness_tracks_state_and_notifies_listeners():
    skill = make_skill()
    provider = FakeProvider(
        [step_response(reasoning="r1", state_delta={"counter": 1}, action="finish", args={})]
    )
    harness = SkillStateHarness(SkillStateHarnessConfig(provider=provider, model="m", skill=skill))
    received = []
    harness.subscribe(received.append)

    events = [event async for event in harness.run("start")]

    assert harness.state["counter"] == 1
    assert not harness.is_running
    assert received == events
    assert isinstance(events[-1], RunEndEvent)


@pytest.mark.asyncio
async def test_harness_rejects_concurrent_run():
    skill = make_skill()
    provider = FakeProvider(
        [step_response(reasoning="r1", state_delta={}, action="finish", args={})]
    )
    harness = SkillStateHarness(SkillStateHarnessConfig(provider=provider, model="m", skill=skill))

    generator = harness.run("start")
    with pytest.raises(RuntimeError):
        harness.run("start-again")

    async for _ in generator:
        pass

    assert not harness.is_running


@pytest.mark.asyncio
async def test_harness_seeds_from_explicit_state_not_only_skill_default():
    skill = make_skill(initial_state={"counter": 0})
    provider = FakeProvider(
        [step_response(reasoning="r1", state_delta={"counter": 6}, action="finish", args={})]
    )
    harness = SkillStateHarness(
        SkillStateHarnessConfig(provider=provider, model="m", skill=skill),
        state={"counter": 5},
    )

    events = [event async for event in harness.run("start")]

    first_call = provider.calls[0]
    assert '"counter": 5' in first_call[2][0].text
    assert harness.state["counter"] == 6
    assert events
