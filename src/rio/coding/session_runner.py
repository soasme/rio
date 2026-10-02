"""Drives CLM runs and writes them to the context journal.

This is the layer between `rio.agent.Harness` and the coding session proper.
It owns what the runtime deliberately does not:

* **Journaling.** Each step is written as a `StepEntry` holding its action,
  its observation, and the full context that resulted, so a resume is one read
  of the newest snapshot. The step's reasoning is journaled separately as a
  `ReasoningEntry` for diagnostics; it already lives in the context.
* **Checkpoints.** Any journaled snapshot can be adopted as the live context.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from rio.agent import (
    ActionEndEvent,
    ActionStartEvent,
    Harness,
    HarnessConfig,
    HarnessSpec,
    ReasoningEvent,
    StepEndEvent,
    StepStartEvent,
    Turn,
    ValidationErrorEvent,
)
from rio.ai.provider import ModelProvider
from rio.coding.events import (
    AgentSettledEvent,
    CodingSessionEvent,
    EntryAppendedEvent,
    SessionRunEndEvent,
    StateRestoredEvent,
)
from rio.coding.session_store import (
    ActionRecord,
    LeafEntry,
    ReasoningEntry,
    SessionEntry,
    SessionStorage,
    StateResetEntry,
    StepEntry,
    ValidationFailureEntry,
    entries_by_id,
    entry_context,
    latest_leaf_id,
)

#: Observations are journaled for auditability, not for replay -- nothing ever
#: reads one back into a prompt. A generous cap keeps a single runaway command's
#: output from dominating the session file.
DEFAULT_JOURNALED_OBSERVATION_LIMIT = 16 * 1024

#: Reasoning is journaled for the same reason. It is prose the model wrote
#: rather than arbitrary program output, so a tighter cap suffices. `0`
#: disables the writes.
DEFAULT_JOURNALED_REASONING_LIMIT = 8 * 1024


@dataclass(slots=True)
class SessionRunnerConfig:
    """Everything a runner needs that does not change between steps."""

    provider: ModelProvider
    model: str
    skill: HarnessSpec
    storage: SessionStorage | None = None
    max_steps: int | None = None
    max_retries: int = 2
    context_window_tokens: int = 128_000
    journaled_observation_limit: int = DEFAULT_JOURNALED_OBSERVATION_LIMIT
    journaled_reasoning_limit: int = DEFAULT_JOURNALED_REASONING_LIMIT
    context_file: Path | None = None


@dataclass(slots=True)
class _StepInProgress:
    """The pieces of one step, gathered across the events that announce them."""

    step: int
    action: ActionRecord | None = None
    observation: str | None = None
    observation_truncated: bool = False


class SessionRunner:
    """Runs one CLM execution against a journal."""

    def __init__(
        self,
        config: SessionRunnerConfig,
        *,
        context: list[Turn] | None = None,
        parent_entry_id: str | None = None,
    ) -> None:
        self._config = config
        self._context: list[Turn] = [dict(item) for item in context or []]
        self._parent_entry_id = parent_entry_id
        self._harness = self._rebuilt_harness()
        self._running = False
        self._steps_this_run = 0
        self._terminated = False
        self._last_observation: str | None = None
        self._answer: str | None = None

    # -- inspection ----------------------------------------------------------

    @property
    def config(self) -> SessionRunnerConfig:
        return self._config

    @property
    def context(self) -> list[Turn]:
        """The context the model sees next. This is the session's entire memory."""
        return [dict(item) for item in self._context]

    @property
    def context_file(self) -> Path:
        """The file the model edits to manage its context."""
        return self._harness.context_file

    @property
    def answer(self) -> str | None:
        """What the last run answered, or `None` if the current turn has not answered yet.

        The answer is the message the terminating action carried.
        """
        return self._answer

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def parent_entry_id(self) -> str | None:
        """The journal entry the next write will hang from."""
        return self._parent_entry_id

    # -- control -------------------------------------------------------------

    def cancel(self) -> None:
        self._harness.cancel()

    # -- running -------------------------------------------------------------

    async def run(self, message: str | None = None) -> AsyncIterator[CodingSessionEvent]:
        """Run the configured skill once with ``message`` in its history."""
        if self._running:
            raise RuntimeError("SessionRunner is already running")
        self._running = True
        self._steps_this_run = 0
        self._terminated = False
        self._answer = None
        try:
            async for event in self._run_once(message):
                yield event

            yield SessionRunEndEvent(
                steps=self._steps_this_run,
                context=self.context,
                answer=self._answer,
            )
            for entry in await self._write_tip_pointer():
                yield EntryAppendedEvent(entry=entry)
            yield AgentSettledEvent()
        finally:
            self._running = False

    async def _run_once(self, message: str | None) -> AsyncIterator[CodingSessionEvent]:
        """Run the loop until it terminates or is cancelled."""
        pending: _StepInProgress | None = None
        async for event in self._harness.run(message):
            if isinstance(event, StepStartEvent):
                pending = _StepInProgress(step=event.step)
                self._context = [dict(item) for item in event.context]
                yield event

            elif isinstance(event, ReasoningEvent):
                # A diagnostic note beside the step: `advance=False` keeps the
                # branch tip on the step chain.
                limit = self._config.journaled_reasoning_limit
                if limit:
                    entry = ReasoningEntry(
                        parent_id=self._parent_entry_id,
                        step=event.step,
                        reasoning=event.reasoning[:limit],
                        truncated=len(event.reasoning) > limit,
                    )
                    for written in await self._write([entry], advance=False):
                        yield EntryAppendedEvent(entry=written)
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
                    self._last_observation = text or None
                yield event

            elif isinstance(event, StepEndEvent):
                self._context = [dict(item) for item in event.context]
                self._steps_this_run += 1
                if pending is not None and pending.action is not None:
                    entry = StepEntry(
                        parent_id=self._parent_entry_id,
                        step=pending.step,
                        state={"context": self.context},
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

            else:
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

    async def load(self) -> list[Turn]:
        """Adopt the context recorded in storage, if any, and return it."""
        if self._config.storage is None:
            return self.context
        entries = await self._config.storage.read_all()
        if not entries:
            return self.context
        leaf = latest_leaf_id(entries)
        if leaf is not None:
            from rio.coding.session_store.tree import state_at_entry

            _state, snapshot = state_at_entry(entries, leaf)
            if snapshot is not None:
                self._context = entry_context(snapshot) or []
        # Hang the next write off the branch tip, not off whatever entry
        # happens to be last in the file -- a pointer is not a parent.
        self._parent_entry_id = latest_leaf_id(entries) or entries[-1].id
        self._harness = self._rebuilt_harness()
        return self.context

    async def restore(self, entry_id: str, *, reason: str | None = None) -> StateRestoredEvent:
        """Adopt a journaled checkpoint as the live context."""
        if self._config.storage is None:
            raise RuntimeError("cannot restore a checkpoint without storage")
        entries = await self._config.storage.read_all()
        entry = entries_by_id(entries).get(entry_id)
        if entry is None:
            raise KeyError(f"no session entry {entry_id!r}")
        context = entry_context(entry)
        if context is None:
            raise ValueError(f"session entry {entry_id!r} carries no context")

        self._context = context
        self._answer = None
        reset = StateResetEntry(
            parent_id=entry_id,
            state={"context": self.context},
            reason=reason,
            restored_from_entry_id=entry_id,
        )
        await self._write([reset])
        await self._write_tip_pointer()
        self._harness = self._rebuilt_harness()
        return StateRestoredEvent(context=self.context, entry_id=entry_id, reason=reason)

    async def reset(self, context: list[Turn] | None = None, *, reason: str | None = None) -> None:
        """Replace the context wholesale, journaling the reset."""
        self._context = [dict(item) for item in context or []]
        self._answer = None
        reset = StateResetEntry(
            parent_id=self._parent_entry_id, state={"context": self.context}, reason=reason
        )
        await self._write([reset])
        await self._write_tip_pointer()
        self._harness = self._rebuilt_harness()

    def rebind(self, *, provider: ModelProvider | None = None, model: str | None = None) -> None:
        """Point the runner at a different provider or model, keeping the context.

        The context is provider-neutral text, so nothing has to be translated.
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
        previous = getattr(self, "_harness", None)
        return Harness(
            HarnessConfig(
                provider=self._config.provider,
                model=self._config.model,
                skill=self._config.skill,
                max_steps=self._config.max_steps,
                max_retries=self._config.max_retries,
                context_window_tokens=self._config.context_window_tokens,
                context_file=(
                    previous.context_file if previous is not None else self._config.context_file
                ),
            ),
            context=self._context,
        )
