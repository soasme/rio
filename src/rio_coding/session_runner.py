"""Drives SKILL.state runs and writes them to the execution-state journal.

This is the layer between `rio_agent.run_skill_loop` and the coding session
proper. It owns three things the runtime deliberately does not:

* **Journaling.** Each accepted step is written as a `StepEntry` holding both
  the merge patch and the resulting state. The model's reasoning is never
  written -- the runtime discards it, and persisting it would rebuild the
  unbounded history the design exists to avoid.
* **Steering.** A message typed while a run is in flight interrupts it and
  restarts it, observing the message alongside the result the run had
  reached. Because the state is a complete description of the run, restarting
  from the current state costs nothing and loses nothing; there is no
  conversation to rewind.
* **Checkpoints.** Any journaled snapshot can be adopted as the live state.
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from rio_agent import (
    ActionEndEvent,
    ActionStartEvent,
    Harness,
    HarnessConfig,
    HarnessObservation,
    HarnessSpec,
    ReasoningDiscardedEvent,
    RunEndEvent,
    RunStartEvent,
    StateUpdateEvent,
    StepEndEvent,
    StepStartEvent,
    ValidationErrorEvent,
    apply_state_delta,
)
from rio_ai.provider import ModelProvider
from rio_ai.types import JSONObject, JSONValue
from rio_coding.events import (
    AgentSettledEvent,
    CodingSessionEvent,
    EntryAppendedEvent,
    QueueUpdateEvent,
    SessionRunEndEvent,
    StateRestoredEvent,
)
from rio_coding.session_store import (
    ActionRecord,
    LeafEntry,
    SessionEntry,
    SessionStorage,
    StateResetEntry,
    StepEntry,
    TurnEntry,
    ValidationFailureEntry,
    entries_by_id,
    entry_state,
    latest_leaf_id,
)

#: Observations are journaled for auditability, not for replay -- nothing ever
#: reads one back into a prompt. A generous cap keeps a single runaway command's
#: output from dominating the session file.
DEFAULT_JOURNALED_OBSERVATION_LIMIT = 16 * 1024


@dataclass(slots=True)
class SessionRunnerConfig:
    """Everything a runner needs that does not change between steps."""

    provider: ModelProvider
    model: str
    skill: HarnessSpec
    storage: SessionStorage | None = None
    max_steps: int | None = None
    max_retries: int = 2
    journaled_observation_limit: int = DEFAULT_JOURNALED_OBSERVATION_LIMIT


@dataclass(slots=True)
class _StepInProgress:
    """The pieces of one step, gathered across the events that announce them."""

    step: int
    state_delta: JSONObject = field(default_factory=dict)
    state: JSONObject = field(default_factory=dict)
    action: ActionRecord | None = None
    observation: str | None = None
    observation_truncated: bool = False


class SessionRunner:
    """Runs SKILL.state turns against a journal, with steering and checkpoints."""

    def __init__(
        self,
        config: SessionRunnerConfig,
        *,
        state: JSONObject | None = None,
        parent_entry_id: str | None = None,
    ) -> None:
        self._config = config
        self._state: JSONObject = dict(state if state is not None else config.skill.initial_state)
        self._parent_entry_id = parent_entry_id
        self._harness = Harness(
            HarnessConfig(
                provider=config.provider,
                model=config.model,
                skill=config.skill,
                max_steps=config.max_steps,
                max_retries=config.max_retries,
            ),
            state=self._state,
        )
        self._steering: deque[str] = deque()
        self._follow_up: deque[str] = deque()
        self._running = False
        self._steps_this_run = 0
        self._terminated = False
        self._last_result: str | None = None
        self._answer: str | None = None

    # -- inspection ----------------------------------------------------------

    @property
    def config(self) -> SessionRunnerConfig:
        return self._config

    @property
    def state(self) -> dict[str, JSONValue]:
        """The complete execution state. This is the session's entire memory."""
        return dict(self._state)

    @property
    def answer(self) -> str | None:
        """What the last run answered, or `None` if the current turn has not answered yet.

        The answer is the message the terminating action carried, not a state
        field: keeping a copy in the state only made it possible for a later
        turn to read an answer that was never its own.
        """
        return self._answer

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def parent_entry_id(self) -> str | None:
        """The journal entry the next write will hang from."""
        return self._parent_entry_id

    @property
    def queued_steering_messages(self) -> tuple[str, ...]:
        return tuple(self._steering)

    @property
    def queued_follow_up_messages(self) -> tuple[str, ...]:
        return tuple(self._follow_up)

    @property
    def queued_message_count(self) -> int:
        return len(self._steering) + len(self._follow_up)

    # -- queues --------------------------------------------------------------

    def queue_steering_message(self, text: str) -> QueueUpdateEvent:
        """Queue text to be folded into the *current* run's next observation."""
        self._steering.append(text)
        return self._queue_event()

    def queue_follow_up_message(self, text: str) -> QueueUpdateEvent:
        """Queue text to start a new run once the current one settles."""
        self._follow_up.append(text)
        return self._queue_event()

    def pop_latest_steering_message(self) -> str | None:
        return self._steering.pop() if self._steering else None

    def pop_latest_follow_up_message(self) -> str | None:
        return self._follow_up.pop() if self._follow_up else None

    def clear_queued_messages(self) -> QueueUpdateEvent:
        self._steering.clear()
        self._follow_up.clear()
        return self._queue_event()

    def _queue_event(self) -> QueueUpdateEvent:
        return QueueUpdateEvent(steering=tuple(self._steering), follow_up=tuple(self._follow_up))

    def _drain_steering(self) -> str | None:
        if not self._steering:
            return None
        merged = "\n".join(self._steering)
        self._steering.clear()
        return merged

    # -- control -------------------------------------------------------------

    def cancel(self) -> None:
        self._harness.cancel()

    # -- running -------------------------------------------------------------

    async def run(self, message: str) -> AsyncIterator[CodingSessionEvent]:
        """Run one turn from a user message, yielding step events and journal writes.

        The turn starts from the state the session has already built, so a
        second message continues the session instead of restarting it. The
        message reaches the model as the observation's user message, not as an
        action result: no action has run yet.

        Steering restarts the underlying loop from the live execution state,
        observing the queued text alongside the result the run had reached.
        That restart is lossless: the state already holds everything the model
        would have been told, so no steps are spent re-establishing context.
        """
        if self._running:
            raise RuntimeError("SessionRunner is already running")
        self._running = True
        self._steps_this_run = 0
        self._terminated = False
        self._answer = None
        try:
            observation = HarnessObservation(user_message=message)
            while True:
                limit = self._config.max_steps
                if limit is not None:
                    remaining = limit - self._steps_this_run
                    if remaining <= 0:
                        break
                    self._harness.config.max_steps = remaining
                async for event in self._run_once(observation):
                    yield event
                steering = self._drain_steering()
                if steering is None or self._terminated:
                    break
                observation = HarnessObservation(
                    user_message=steering, tool_call_result=self._last_result
                )

            yield SessionRunEndEvent(
                steps=self._steps_this_run,
                state=dict(self._state),
                answer=self._answer,
            )
            for entry in await self._write_tip_pointer():
                yield EntryAppendedEvent(entry=entry)
            if not self._follow_up:
                yield AgentSettledEvent()
        finally:
            self._running = False

    async def _run_once(self, observation: HarnessObservation) -> AsyncIterator[CodingSessionEvent]:
        """Run the loop until it terminates, or until steering interrupts it.

        Interrupting is a cancel at a step boundary. Nothing has to be saved
        before cancelling: the state up to the last committed step is already
        the whole of what a restarted loop would be told.
        """
        self._last_result = observation.tool_call_result
        pending: _StepInProgress | None = None
        turn_written = False

        iterator = self._harness.run(observation)
        async for event in iterator:
            if isinstance(event, RunStartEvent):
                if not turn_written and observation.user_message is not None:
                    turn_written = True
                    entry = TurnEntry(
                        parent_id=self._parent_entry_id,
                        observation=observation.user_message,
                        state=dict(self._state),
                    )
                    for written in await self._write([entry]):
                        yield EntryAppendedEvent(entry=written)
                yield event

            elif isinstance(event, StepStartEvent):
                pending = _StepInProgress(step=event.step, state=dict(event.state))
                yield event

            elif isinstance(event, ReasoningDiscardedEvent):
                # Surfaced once for observability, then gone. Never journaled:
                # a step's reasoning is not part of what the next step is told.
                yield event

            elif isinstance(event, ValidationErrorEvent):
                entry = ValidationFailureEntry(
                    parent_id=self._parent_entry_id,
                    step=event.step,
                    attempt=event.attempt,
                    error=event.error,
                )
                for written in await self._write([entry], advance=False):
                    yield EntryAppendedEvent(entry=written)
                yield event

            elif isinstance(event, StateUpdateEvent):
                if pending is not None:
                    # A step can commit twice: the model's own delta, then the
                    # runtime's record of what the action produced. The journal
                    # keeps one patch per step, so the second folds into the
                    # first rather than replacing it.
                    pending.state_delta = apply_state_delta(pending.state_delta, event.delta)
                    pending.state = dict(event.state)
                self._state = dict(event.state)
                yield event

            elif isinstance(event, ActionStartEvent):
                if pending is not None:
                    pending.action = ActionRecord(name=event.name, arguments=dict(event.arguments))
                yield event

            elif isinstance(event, ActionEndEvent):
                text = event.result.text or ""
                if event.result.terminate and not event.is_error:
                    self._answer = text or None
                if pending is not None:
                    limit = self._config.journaled_observation_limit
                    pending.observation_truncated = len(text) > limit
                    pending.observation = text[:limit]
                    self._last_result = text or None
                yield event

            elif isinstance(event, StepEndEvent):
                self._state = dict(event.state)
                self._steps_this_run += 1
                if pending is not None and pending.action is not None:
                    entry = StepEntry(
                        parent_id=self._parent_entry_id,
                        step=pending.step,
                        state_delta=pending.state_delta,
                        state=pending.state or dict(event.state),
                        action=pending.action,
                        observation=pending.observation,
                        observation_truncated=pending.observation_truncated,
                        terminated=event.terminated,
                    )
                    for written in await self._write([entry]):
                        yield EntryAppendedEvent(entry=written)
                pending = None
                self._terminated = event.terminated
                yield event
                if self._steering and not event.terminated:
                    # Interrupt at the step boundary. The loop sees the cancel
                    # on its next iteration and unwinds cleanly; `run` then
                    # restarts it from this same state with the steering text
                    # appended to the observation.
                    self._harness.cancel()

            elif isinstance(event, RunEndEvent):
                self._state = dict(event.state)
                yield event

            else:  # pragma: no cover - defensive against new runtime events
                yield event

    # -- journal -------------------------------------------------------------

    async def _write(
        self, entries: list[SessionEntry], *, advance: bool = True
    ) -> list[SessionEntry]:
        """Append entries and, unless told otherwise, move the branch tip."""
        tip = entries[-1].id if advance and entries else None
        batch = list(entries)
        if tip is not None:
            # Commit the checkpoint and active tip together, even if the next
            # provider request fails before the run's final events are emitted.
            batch.append(LeafEntry(parent_id=tip, entry_id=tip))
        if self._config.storage is not None:
            await self._config.storage.append_batch(batch)
        if tip is not None:
            self._parent_entry_id = tip
        return batch

    async def append_entries(self, entries: list[SessionEntry]) -> None:
        """Attach session metadata to the active branch and publish its tip."""
        parent = self._parent_entry_id
        for entry in entries:
            if entry.parent_id is None:
                entry.parent_id = parent
            parent = entry.id
        if not entries:
            return
        pointer = LeafEntry(parent_id=parent, entry_id=parent)
        await self._write([*entries, pointer], advance=False)
        self._parent_entry_id = parent

    async def _write_tip_pointer(self) -> list[SessionEntry]:
        """Publish the current branch tip so a later resume finds it.

        Every state-changing write must land a fresh pointer, otherwise a
        stale one from an earlier run keeps naming the old branch and a
        resume silently adopts state the session has already moved past.
        """
        tip = self._parent_entry_id
        return await self._write([LeafEntry(parent_id=tip, entry_id=tip)], advance=False)

    async def load(self) -> JSONObject:
        """Adopt the state recorded in storage, if any, and return it."""
        if self._config.storage is None:
            return self.state
        entries = await self._config.storage.read_all()
        if not entries:
            return self.state
        leaf = latest_leaf_id(entries)
        if leaf is not None:
            from rio_coding.session_store.tree import state_at_entry

            state, snapshot = state_at_entry(entries, leaf)
            if snapshot is not None:
                self._state = state
        # Hang the next write off the branch tip, not off whatever entry
        # happens to be last in the file -- a pointer is not a parent.
        self._parent_entry_id = latest_leaf_id(entries) or entries[-1].id
        self._harness = self._rebuilt_harness()
        return self.state

    async def restore(self, entry_id: str, *, reason: str | None = None) -> StateRestoredEvent:
        """Adopt a journaled checkpoint as the live execution state.

        This is the whole of rio's branching story. There is no transcript to
        truncate and nothing to replay: the checkpoint *is* the state.
        """
        if self._config.storage is None:
            raise RuntimeError("cannot restore a checkpoint without storage")
        entries = await self._config.storage.read_all()
        entry = entries_by_id(entries).get(entry_id)
        if entry is None:
            raise KeyError(f"no session entry {entry_id!r}")
        state = entry_state(entry)
        if state is None:
            raise ValueError(f"session entry {entry_id!r} carries no execution state")

        self._state = state
        self._answer = None
        reset = StateResetEntry(
            parent_id=entry_id,
            state=dict(state),
            reason=reason,
            restored_from_entry_id=entry_id,
        )
        await self._write([reset])
        await self._write_tip_pointer()
        self._harness = self._rebuilt_harness()
        return StateRestoredEvent(state=dict(state), entry_id=entry_id, reason=reason)

    async def reset(self, state: JSONObject | None = None, *, reason: str | None = None) -> None:
        """Replace the execution state wholesale, journaling the reset."""
        self._state = dict(state if state is not None else self._config.skill.initial_state)
        self._answer = None
        reset = StateResetEntry(
            parent_id=self._parent_entry_id, state=dict(self._state), reason=reason
        )
        await self._write([reset])
        await self._write_tip_pointer()
        self._harness = self._rebuilt_harness()

    def rebind(self, *, provider: ModelProvider | None = None, model: str | None = None) -> None:
        """Point the runner at a different provider or model, keeping the state.

        A model swap is trivial here. There is no transcript to translate
        between provider message formats -- the next step's prompt is rebuilt
        from the state regardless of who ran the previous one.
        """
        if provider is not None:
            self._config.provider = provider
        if model is not None:
            self._config.model = model
        self._harness = self._rebuilt_harness()

    def rebind_skill(self, skill: HarnessSpec) -> None:
        """Swap the skill specification (new instructions, tools, or schema)."""
        self._config.skill = skill
        self._harness = self._rebuilt_harness()

    def _rebuilt_harness(self) -> Harness:
        return Harness(
            HarnessConfig(
                provider=self._config.provider,
                model=self._config.model,
                skill=self._config.skill,
                max_steps=self._config.max_steps,
                max_retries=self._config.max_retries,
            ),
            state=self._state,
        )
