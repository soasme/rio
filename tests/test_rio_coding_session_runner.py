"""Tests for `SessionRunner`: SKILL.state runs journaled as execution state.

The assertions worth reading here are the ones that only hold because there is
no transcript: journaled reasoning never reaches a prompt or a resume, a model
swap mid-session needs no history translation, steering restarts a run for
free, and the prompt the provider receives does not grow with the step count.
"""

from __future__ import annotations

import pytest

from conftest import step_response
from rio.agent import HarnessSpec, ReasoningDiscardedEvent, StepEndEvent
from rio.ai import AgentTool, AgentToolResult, FakeProvider, TextContent
from rio.coding.events import (
    AgentSettledEvent,
    EntryAppendedEvent,
    QueueUpdateEvent,
    SessionRunEndEvent,
)
from rio.coding.session_runner import SessionRunner, SessionRunnerConfig
from rio.coding.session_store import (
    InMemorySessionStorage,
    ReasoningEntry,
    StepEntry,
    TurnEntry,
    ValidationFailureEntry,
    latest_leaf_id,
    resume_state,
)

CODING_FIELDS = ("goal", "plan", "findings", "files", "cwd", "last_error")


async def _read(tool_call_id, arguments, signal=None, on_update=None):
    path = arguments.get("path", "")
    return AgentToolResult(content=[TextContent(text=f"read {path}\ncontents of {path}")])


async def _respond(tool_call_id, arguments, signal=None, on_update=None):
    return AgentToolResult(
        content=[TextContent(text=str(arguments.get("message", "")))], terminate=True
    )


READ = AgentTool(
    name="read",
    label="Read",
    description="Read a file.",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    execute_fn=_read,
)
RESPOND = AgentTool(
    name="respond",
    label="Respond",
    description="Answer the user and end the turn.",
    parameters={"type": "object", "properties": {"message": {"type": "string"}}},
    execute_fn=_respond,
)


def coding_skill(**overrides) -> HarnessSpec:
    defaults = dict(
        name="rio-coding",
        instructions="You are rio, a coding agent driven by SKILL.state.",
        state_fields=CODING_FIELDS,
        initial_state={"goal": "", "plan": [], "findings": {}, "files": {}, "cwd": "/repo"},
        actions=(READ, RESPOND),
    )
    defaults.update(overrides)
    return HarnessSpec(**defaults)


def make_runner(streams, *, storage=None, **config_overrides):
    provider = FakeProvider(streams)
    config = SessionRunnerConfig(
        provider=provider,
        model="test-model",
        skill=coding_skill(),
        storage=storage,
        **config_overrides,
    )
    return SessionRunner(config), provider


async def collect(runner, observation):
    return [event async for event in runner.run(observation)]


def two_step_streams():
    return [
        step_response(
            reasoning="I should look at the entry point first.",
            state_delta={"goal": "explain main.py", "files": {"main.py": {"status": "read"}}},
            action="read",
            args={"path": "main.py"},
        ),
        step_response(
            reasoning="Now I can answer.",
            state_delta={"plan": [{"id": "1", "title": "explain", "status": "done"}]},
            action="respond",
            args={"message": "main.py starts the server."},
        ),
    ]


class TestRunLifecycle:
    async def test_a_turn_runs_to_a_terminating_action(self) -> None:
        runner, _ = make_runner(two_step_streams())
        events = await collect(runner, "explain main.py")

        run_end = next(e for e in events if isinstance(e, SessionRunEndEvent))
        assert run_end.steps == 2
        assert run_end.answer == "main.py starts the server."
        assert runner.state["files"] == {"main.py": {"status": "read"}}

    async def test_the_session_settles_when_nothing_is_queued(self) -> None:
        runner, _ = make_runner(two_step_streams())
        events = await collect(runner, "explain main.py")
        assert isinstance(events[-1], AgentSettledEvent)

    async def test_a_queued_follow_up_defers_settling(self) -> None:
        runner, _ = make_runner(two_step_streams())
        runner.queue_follow_up_message("and then run the tests")
        events = await collect(runner, "explain main.py")
        assert not any(isinstance(e, AgentSettledEvent) for e in events)

    async def test_a_second_concurrent_run_is_refused(self) -> None:
        runner, _ = make_runner(two_step_streams())
        iterator = runner.run("first")
        await anext(iterator)
        with pytest.raises(RuntimeError, match="already running"):
            await anext(runner.run("second"))
        await iterator.aclose()


class TestJournal:
    async def test_each_step_is_journaled_with_its_patch_and_result(self) -> None:
        storage = InMemorySessionStorage()
        runner, _ = make_runner(two_step_streams(), storage=storage)
        await collect(runner, "explain main.py")

        entries = await storage.read_all()
        turns = [e for e in entries if isinstance(e, TurnEntry)]
        steps = [e for e in entries if isinstance(e, StepEntry)]

        assert [t.observation for t in turns] == ["explain main.py"]
        assert [s.action.name for s in steps] == ["read", "respond"]
        assert steps[0].state_delta["goal"] == "explain main.py"
        assert steps[0].state["files"] == {"main.py": {"status": "read"}}
        assert steps[-1].terminated is True

    async def test_the_journal_is_a_parent_linked_chain(self) -> None:
        storage = InMemorySessionStorage()
        runner, _ = make_runner(two_step_streams(), storage=storage)
        await collect(runner, "explain main.py")

        entries = await storage.read_all()
        # Leaf pointers and reasoning notes hang off the chain without
        # extending it; the chain itself is the state-carrying entries.
        entries = [entry for entry in entries if entry.type not in {"leaf", "reasoning"}]
        for previous, current in zip(entries, entries[1:], strict=False):
            assert current.parent_id == previous.id

    async def test_reasoning_is_surfaced_once_and_journaled_beside_its_step(self) -> None:
        """Discarded from the prompt, kept in the journal as a leaf note."""
        storage = InMemorySessionStorage()
        runner, _ = make_runner(two_step_streams(), storage=storage)
        events = await collect(runner, "explain main.py")

        surfaced = [e for e in events if isinstance(e, ReasoningDiscardedEvent)]
        assert [e.reasoning for e in surfaced] == [
            "I should look at the entry point first.",
            "Now I can answer.",
        ]

        entries = await storage.read_all()
        reasoning = [e for e in entries if isinstance(e, ReasoningEntry)]
        assert [(e.step, e.reasoning, e.truncated) for e in reasoning] == [
            (0, "I should look at the entry point first.", False),
            (1, "Now I can answer.", False),
        ]

        # Each note sits immediately before the step it explains, and shares
        # that step's parent: it is beside the chain, not in it.
        for note in reasoning:
            index = entries.index(note)
            step = entries[index + 1]
            assert isinstance(step, StepEntry)
            assert step.step == note.step
            assert step.parent_id == note.parent_id

    async def test_reasoning_journaling_can_be_switched_off(self) -> None:
        """Reasoning at rest is opt-out with one setting."""
        storage = InMemorySessionStorage()
        runner, _ = make_runner(two_step_streams(), storage=storage, journaled_reasoning_limit=0)
        await collect(runner, "explain main.py")

        entries = await storage.read_all()
        assert [e for e in entries if isinstance(e, ReasoningEntry)] == []
        assert "I should look at the entry point first." not in repr(entries)

    async def test_long_reasoning_is_truncated(self) -> None:
        streams = [
            step_response(reasoning="r" * 500, state_delta={}, action="read", args={"path": "a"}),
            step_response(reasoning="ok", state_delta={}, action="respond", args={"message": "ok"}),
        ]
        storage = InMemorySessionStorage()
        runner, _ = make_runner(streams, storage=storage, journaled_reasoning_limit=100)
        await collect(runner, "explain main.py")

        first, second = [e for e in await storage.read_all() if isinstance(e, ReasoningEntry)]
        assert first.truncated is True
        assert len(first.reasoning) == 100
        assert second.truncated is False

    async def test_a_journaled_session_resumes_to_the_same_state_as_one_without(self) -> None:
        """Reasoning entries carry no state, so resume cannot see them."""
        with_reasoning = InMemorySessionStorage()
        without_reasoning = InMemorySessionStorage()
        runner, _ = make_runner(two_step_streams(), storage=with_reasoning)
        await collect(runner, "explain main.py")
        bare, _ = make_runner(
            two_step_streams(), storage=without_reasoning, journaled_reasoning_limit=0
        )
        await collect(bare, "explain main.py")

        journaled_entries = await with_reasoning.read_all()
        assert any(isinstance(e, ReasoningEntry) for e in journaled_entries)
        assert resume_state(journaled_entries) == resume_state(await without_reasoning.read_all())

        resumed, _ = make_runner([], storage=with_reasoning)
        assert await resumed.load() == runner.state
        # The branch tip still names the last committed step, not a note.
        leaf = latest_leaf_id(journaled_entries)
        assert leaf == next(e.id for e in reversed(journaled_entries) if isinstance(e, StepEntry))
        assert resumed.parent_entry_id == leaf

    async def test_entry_appended_events_mirror_the_journal(self) -> None:
        storage = InMemorySessionStorage()
        runner, _ = make_runner(two_step_streams(), storage=storage)
        events = await collect(runner, "explain main.py")

        announced = [e.entry.id for e in events if isinstance(e, EntryAppendedEvent)]
        assert announced == [e.id for e in await storage.read_all()]

    async def test_a_rejected_proposal_is_journaled_but_does_not_advance_the_state(self) -> None:
        storage = InMemorySessionStorage()
        streams = [
            step_response(
                reasoning="bad", state_delta={"nonexistent": 1}, action="read", args={"path": "a"}
            ),
            *two_step_streams(),
        ]
        runner, _ = make_runner(streams, storage=storage)
        await collect(runner, "explain main.py")

        entries = await storage.read_all()
        failures = [e for e in entries if isinstance(e, ValidationFailureEntry)]
        assert len(failures) == 1
        assert "nonexistent" in failures[0].error
        assert "nonexistent" not in runner.state

    async def test_a_long_observation_is_truncated_in_the_journal_only(self) -> None:
        storage = InMemorySessionStorage()
        streams = [
            step_response(reasoning="", state_delta={}, action="read", args={"path": "x" * 4000}),
            step_response(reasoning="", state_delta={}, action="respond", args={"message": "ok"}),
        ]
        runner, _ = make_runner(streams, storage=storage, journaled_observation_limit=100)
        await collect(runner, "read a huge file")

        step = next(e for e in await storage.read_all() if isinstance(e, StepEntry))
        assert step.observation_truncated is True
        assert len(step.observation) == 100


class TestBoundedPrompt:
    async def test_the_prompt_does_not_grow_with_the_step_count(self) -> None:
        """The paper's core claim, checked at the session layer.

        Twenty steps that each add one finding to a bounded state must not
        produce twenty prompts of increasing size. A transcript-based agent
        would grow every one of them by the previous step's observation.
        """
        steps = 20
        streams = [
            step_response(
                reasoning="x" * 500,
                state_delta={"scratch_note": None} if False else {},
                action="read",
                args={"path": f"file{index}.py"},
            )
            for index in range(steps)
        ]
        streams.append(
            step_response(
                reasoning="done",
                state_delta={},
                action="respond",
                args={"message": "done"},
            )
        )
        runner, provider = make_runner(streams)
        await collect(runner, "survey the repository")

        # One provider call per step; each carries exactly one user message.
        assert all(len(messages) == 1 for _model, _system, messages, _tools in provider.calls)

        sizes = [len(messages[0].content) for _m, _s, messages, _t in provider.calls]
        # State is unchanged across these steps, so the only variation is the
        # observation, whose length is bounded by the tool's own output.
        assert max(sizes) - min(sizes) < 200
        assert sizes[-1] < sizes[0] * 2

    async def test_journaling_reasoning_does_not_change_any_prompt(self) -> None:
        """The journal is write-only with respect to the prompt.

        Two identical runs, one journaling reasoning and one not, must send
        byte-identical prompts: journaling changes what is on disk, never what
        the next step is told.
        """
        storage = InMemorySessionStorage()
        journaling, journaling_provider = make_runner(two_step_streams(), storage=storage)
        await collect(journaling, "explain main.py")
        bare, bare_provider = make_runner(two_step_streams(), journaled_reasoning_limit=0)
        await collect(bare, "explain main.py")

        assert any(isinstance(e, ReasoningEntry) for e in await storage.read_all())
        assert [
            [message.content for message in messages]
            for _m, _s, messages, _t in journaling_provider.calls
        ] == [
            [message.content for message in messages]
            for _m, _s, messages, _t in bare_provider.calls
        ]

    async def test_no_call_replays_an_earlier_observation(self) -> None:
        """The second step's prompt carries only the newest observation.

        The user's original request still appears, but only because the model
        chose to write it into the `goal` state field -- it survives as state,
        not as replayed history.
        """
        runner, provider = make_runner(two_step_streams())
        await collect(runner, "explain main.py")

        _model, _system, second_messages, _tools = provider.calls[1]
        _state_section, _, observation = second_messages[0].content.partition("Latest Observation:")
        assert "explain main.py" not in observation
        assert "contents of main.py" in observation


class TestSteering:
    async def test_steering_arrives_as_a_user_message_beside_the_observation(self) -> None:
        streams = [
            step_response(reasoning="", state_delta={}, action="read", args={"path": "main.py"}),
            step_response(reasoning="", state_delta={}, action="respond", args={"message": "ok"}),
        ]
        runner, provider = make_runner(streams)

        events = []
        async for event in runner.run("explain main.py"):
            events.append(event)
            if isinstance(event, StepEndEvent) and event.step == 0:
                runner.queue_steering_message("actually, focus on error handling")

        steered = next(
            messages[0].content
            for _m, _s, messages, _t in provider.calls
            if "actually, focus on error handling" in messages[0].content
        )
        assert "New User Message:\nactually, focus on error handling" in steered
        # It reads the run's own last observation in the same prompt.
        assert "Latest Observation:\nread main.py" in steered

    async def test_steering_restarts_from_the_live_state_not_from_scratch(self) -> None:
        """Restarting is free because the state already holds the run's findings."""
        streams = [
            step_response(
                reasoning="",
                state_delta={"findings": {"entrypoint": "main.py:12"}},
                action="read",
                args={"path": "main.py"},
            ),
            step_response(reasoning="", state_delta={}, action="respond", args={"message": "ok"}),
        ]
        runner, provider = make_runner(streams)

        async for event in runner.run("explain main.py"):
            if isinstance(event, StepEndEvent) and event.step == 0:
                runner.queue_steering_message("focus on error handling")

        # The prompt that carries the steering text still carries the finding
        # discovered before the interruption -- nothing was re-derived.
        steered = next(
            messages[0].content
            for _m, _s, messages, _t in provider.calls
            if "focus on error handling" in messages[0].content
        )
        assert "main.py:12" in steered

    async def test_queue_events_report_both_queues(self) -> None:
        runner, _ = make_runner([])
        runner.queue_steering_message("a")
        event = runner.queue_follow_up_message("b")
        assert isinstance(event, QueueUpdateEvent)
        assert event.steering == ("a",)
        assert event.follow_up == ("b",)
        assert runner.queued_message_count == 2

        assert runner.pop_latest_follow_up_message() == "b"
        assert runner.pop_latest_steering_message() == "a"
        assert runner.pop_latest_steering_message() is None

        runner.queue_steering_message("c")
        assert runner.clear_queued_messages().steering == ()


class TestFollowUpTurns:
    """A second message continues the session; it does not restart it."""

    async def _first_turn(self, storage=None):
        runner, _ = make_runner(
            [
                step_response(
                    reasoning="",
                    state_delta={
                        "goal": "explain main.py",
                        "findings": {"entrypoint": "main.py:12"},
                        "last_error": "read failed once",
                    },
                    action="read",
                    args={"path": "main.py"},
                ),
                step_response(
                    reasoning="",
                    state_delta={},
                    action="respond",
                    args={"message": "main.py starts the server."},
                ),
            ],
            storage=storage,
        )
        await collect(runner, "explain main.py")
        return runner

    def _second_turn(self, runner):
        provider = FakeProvider(
            [
                step_response(
                    reasoning="",
                    state_delta={"goal": "explain error handling"},
                    action="respond",
                    args={"message": "it handles retries"},
                )
            ]
        )
        runner.rebind(provider=provider)
        return provider

    async def test_a_follow_up_keeps_what_the_session_already_learned(self) -> None:
        runner = await self._first_turn()
        provider = self._second_turn(runner)

        await collect(runner, "what about error handling")

        # The follow-up opens with turn one's finding already in hand.
        assert "main.py:12" in provider.calls[0][2][0].content
        assert runner.state["findings"] == {"entrypoint": "main.py:12"}

    async def test_a_follow_up_asks_as_a_user_not_as_a_tool_result(self) -> None:
        runner = await self._first_turn()
        provider = self._second_turn(runner)

        await collect(runner, "what about error handling")

        body = provider.calls[0][2][0].content
        assert "New User Message:\nwhat about error handling" in body
        assert "Latest Observation:" not in body

    async def test_the_answer_is_the_turn_that_gave_it(self) -> None:
        runner = await self._first_turn()
        assert runner.answer == "main.py starts the server."

        self._second_turn(runner)
        events = await collect(runner, "what about error handling")

        run_end = next(e for e in events if isinstance(e, SessionRunEndEvent))
        assert run_end.answer == "it handles retries"
        assert runner.answer == "it handles retries"

    async def test_a_turn_that_never_answers_reports_no_answer(self) -> None:
        runner = await self._first_turn()
        runner.rebind(provider=FakeProvider([]))

        with pytest.raises(RuntimeError):
            await collect(runner, "what about error handling")
        assert runner.answer is None

    async def test_the_message_is_journaled_as_the_user_typed_it(self) -> None:
        storage = InMemorySessionStorage()
        runner = await self._first_turn(storage=storage)
        self._second_turn(runner)
        await collect(runner, "what about error handling")

        turns = [e for e in await storage.read_all() if isinstance(e, TurnEntry)]
        assert [t.observation for t in turns] == [
            "explain main.py",
            "what about error handling",
        ]


class TestCheckpoints:
    async def test_load_resumes_from_the_newest_snapshot(self) -> None:
        storage = InMemorySessionStorage()
        runner, _ = make_runner(two_step_streams(), storage=storage)
        await collect(runner, "explain main.py")

        resumed, _ = make_runner([], storage=storage)
        state = await resumed.load()
        assert state["files"] == {"main.py": {"status": "read"}}
        assert state == resume_state(await storage.read_all())

    async def test_restore_adopts_an_earlier_checkpoint(self) -> None:
        storage = InMemorySessionStorage()
        runner, _ = make_runner(two_step_streams(), storage=storage)
        await collect(runner, "explain main.py")

        first_step = next(e for e in await storage.read_all() if isinstance(e, StepEntry))
        event = await runner.restore(first_step.id, reason="rewind")

        assert event.entry_id == first_step.id
        assert runner.state == first_step.state
        assert runner.answer is None
        assert await storage.read_all() != []

    async def test_a_restored_checkpoint_survives_a_reload(self) -> None:
        """Restoring must republish the branch tip, or a resume reads the old one."""
        storage = InMemorySessionStorage()
        runner, _ = make_runner(two_step_streams(), storage=storage)
        await collect(runner, "explain main.py")

        first_step = next(e for e in await storage.read_all() if isinstance(e, StepEntry))
        await runner.restore(first_step.id, reason="rewind")

        reloaded, _ = make_runner([], storage=storage)
        assert (await reloaded.load())["plan"] == []

    async def test_restoring_a_non_checkpoint_entry_is_refused(self) -> None:
        storage = InMemorySessionStorage()
        streams = [
            step_response(
                reasoning="", state_delta={"bogus": 1}, action="read", args={"path": "a"}
            ),
            *two_step_streams(),
        ]
        runner, _ = make_runner(streams, storage=storage)
        await collect(runner, "go")

        failure = next(e for e in await storage.read_all() if isinstance(e, ValidationFailureEntry))
        with pytest.raises(ValueError, match="carries no execution state"):
            await runner.restore(failure.id)

    async def test_restoring_an_unknown_entry_is_refused(self) -> None:
        runner, _ = make_runner([], storage=InMemorySessionStorage())
        with pytest.raises(KeyError):
            await runner.restore("nope")

    async def test_restore_requires_storage(self) -> None:
        runner, _ = make_runner([])
        with pytest.raises(RuntimeError, match="without storage"):
            await runner.restore("anything")

    async def test_reset_clears_the_state_and_journals_it(self) -> None:
        storage = InMemorySessionStorage()
        runner, _ = make_runner(two_step_streams(), storage=storage)
        await collect(runner, "explain main.py")

        await runner.reset(reason="new session")
        assert runner.answer is None
        assert runner.state["plan"] == []
        assert runner.state["cwd"] == "/repo"
        assert resume_state(await storage.read_all()) == runner.state


class TestRebinding:
    async def test_swapping_models_keeps_the_state_and_needs_no_translation(self) -> None:
        """No transcript means no cross-provider history to convert."""
        storage = InMemorySessionStorage()
        runner, _ = make_runner(
            [
                step_response(
                    reasoning="",
                    state_delta={"findings": {"lang": "python"}},
                    action="read",
                    args={"path": "main.py"},
                ),
                step_response(
                    reasoning="",
                    state_delta={},
                    action="respond",
                    args={"message": "python"},
                ),
            ],
            storage=storage,
        )
        await collect(runner, "what language is this")

        other = FakeProvider(
            [
                step_response(
                    reasoning="",
                    state_delta={},
                    action="respond",
                    args={"message": "still python"},
                )
            ]
        )
        runner.rebind(provider=other, model="another-model")
        await collect(runner, "are you sure")

        _model, _system, messages, _tools = other.calls[0]
        # The new provider's very first prompt already carries the finding the
        # previous provider discovered, because it is in the state.
        assert "python" in messages[0].content
        assert runner.config.model == "another-model"

    async def test_rebinding_the_skill_swaps_instructions(self) -> None:
        runner, _ = make_runner([])
        runner.rebind_skill(coding_skill(instructions="Totally new instructions."))
        assert runner.config.skill.instructions == "Totally new instructions."


async def test_resume_uses_last_committed_step_when_later_provider_request_fails():
    storage = InMemorySessionStorage()
    runner, _ = make_runner(two_step_streams(), storage=storage)
    await collect(runner, "first turn")
    runner.rebind(
        provider=FakeProvider(
            [step_response(reasoning="", state_delta={"goal": "new goal"}, action="read", args={})]
        )
    )
    with pytest.raises(RuntimeError):
        await collect(runner, "second turn")
    resumed, _ = make_runner([], storage=storage)
    assert (await resumed.load())["goal"] == "new goal"


async def test_steering_restarts_share_the_turn_step_limit():
    streams = [
        step_response(reasoning="", state_delta={}, action="read", args={}) for _ in range(5)
    ]
    runner, provider = make_runner(streams, max_steps=2)
    events = []
    async for event in runner.run("begin"):
        events.append(event)
        if isinstance(event, StepEndEvent):
            runner.queue_steering_message("keep checking")
    assert len(provider.calls) == 2
    assert next(event for event in events if isinstance(event, SessionRunEndEvent)).steps == 2
