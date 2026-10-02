"""Drives notebook runs and writes them to the journal.

This is the layer between `rio.agent.Harness` and the coding session proper.
It owns what the runtime deliberately does not:

* **Journaling.** Each step is written as a `StepEntry` holding the JSON Patch
  from the last journaled notebook to the new one, so the notebook is always
  derived from the session file. The step's reasoning is journaled separately
  as a `ReasoningEntry` for diagnostics; it is not in the notebook.
* **Checkpoints.** The notebook at any journaled step can be adopted as the
  live one.
"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from dataclasses import dataclass

from rio.agent import (
    Harness,
    HarnessConfig,
    HarnessSpec,
    Notebook,
    PatchEvent,
    ReasoningEvent,
    StepEndEvent,
    StepStartEvent,
    ValidationErrorEvent,
    diff,
    new_notebook,
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
    LeafEntry,
    ReasoningEntry,
    SessionEntry,
    SessionStorage,
    StateResetEntry,
    StepEntry,
    ValidationFailureEntry,
    entries_by_id,
    latest_leaf_id,
    notebook_at_entry,
)

#: Reasoning is journaled for diagnostics, never read back into a prompt. `0`
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
    journaled_reasoning_limit: int = DEFAULT_JOURNALED_REASONING_LIMIT


class SessionRunner:
    """Runs one CLM execution against a journal."""

    def __init__(
        self,
        config: SessionRunnerConfig,
        *,
        notebook: Notebook | None = None,
        parent_entry_id: str | None = None,
    ) -> None:
        self._config = config
        self._notebook: Notebook = copy.deepcopy(notebook) if notebook else new_notebook()
        #: The notebook the journal ends at; the next step patch starts here.
        self._journaled: Notebook = copy.deepcopy(self._notebook)
        self._parent_entry_id = parent_entry_id
        self._harness = self._rebuilt_harness()
        self._running = False
        self._steps_this_run = 0
        self._terminated = False
        self._answer: str | None = None

    # -- inspection ----------------------------------------------------------

    @property
    def config(self) -> SessionRunnerConfig:
        return self._config

    @property
    def notebook(self) -> Notebook:
        """The notebook the model sees next. This is the session's entire memory."""
        return copy.deepcopy(self._notebook)

    @property
    def answer(self) -> str | None:
        """What the last run answered, or `None` if the current turn has not answered yet.

        The answer is the `reply` of the step that ended the run.
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
        """Run the configured skill once with ``message`` as a new user cell."""
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
                notebook=self.notebook,
                answer=self._answer,
            )
            for entry in await self._write_tip_pointer():
                yield EntryAppendedEvent(entry=entry)
            yield AgentSettledEvent()
        finally:
            self._running = False

    async def _run_once(self, message: str | None) -> AsyncIterator[CodingSessionEvent]:
        """Run the loop until it terminates or is cancelled."""
        cells: list[int] = []
        async for event in self._harness.run(message):
            if isinstance(event, StepStartEvent):
                self._notebook = copy.deepcopy(event.notebook)
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

            elif isinstance(event, PatchEvent):
                cells = list(event.cells)
                yield event

            elif isinstance(event, StepEndEvent):
                self._notebook = copy.deepcopy(event.notebook)
                self._steps_this_run += 1
                if event.reply is not None:
                    self._answer = event.reply
                entry = StepEntry(
                    parent_id=self._parent_entry_id,
                    step=event.step,
                    patch=diff(self._journaled, self._notebook),
                    cells=cells,
                    reply=event.reply,
                )
                self._journaled = self.notebook
                for written in await self._write([entry]):
                    yield EntryAppendedEvent(entry=written)
                cells = []
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

    async def load(self) -> Notebook:
        """Adopt the notebook recorded in storage, if any, and return it."""
        if self._config.storage is None:
            return self.notebook
        entries = await self._config.storage.read_all()
        if not entries:
            return self.notebook
        # Hang the next write off the branch tip, not off whatever entry
        # happens to be last in the file -- a pointer is not a parent.
        self._parent_entry_id = latest_leaf_id(entries) or entries[-1].id
        self._adopt(notebook_at_entry(entries, self._parent_entry_id))
        return self.notebook

    async def restore(self, entry_id: str, *, reason: str | None = None) -> StateRestoredEvent:
        """Adopt the notebook at a journaled checkpoint as the live one."""
        if self._config.storage is None:
            raise RuntimeError("cannot restore a checkpoint without storage")
        entries = await self._config.storage.read_all()
        if entry_id not in entries_by_id(entries):
            raise KeyError(f"no session entry {entry_id!r}")
        self._adopt(notebook_at_entry(entries, entry_id))
        self._answer = None
        reset = StateResetEntry(
            parent_id=entry_id,
            notebook=self.notebook,
            reason=reason,
            restored_from_entry_id=entry_id,
        )
        await self._write([reset])
        await self._write_tip_pointer()
        return StateRestoredEvent(notebook=self.notebook, entry_id=entry_id, reason=reason)

    async def reset(self, notebook: Notebook | None = None, *, reason: str | None = None) -> None:
        """Replace the notebook wholesale, journaling the reset."""
        self._adopt(notebook if notebook is not None else new_notebook())
        self._answer = None
        reset = StateResetEntry(
            parent_id=self._parent_entry_id, notebook=self.notebook, reason=reason
        )
        await self._write([reset])
        await self._write_tip_pointer()

    def _adopt(self, notebook: Notebook) -> None:
        self._notebook = copy.deepcopy(notebook)
        self._journaled = copy.deepcopy(notebook)
        self._harness = self._rebuilt_harness()

    def rebind(self, *, provider: ModelProvider | None = None, model: str | None = None) -> None:
        """Point the runner at a different provider or model, keeping the notebook.

        The notebook is provider-neutral JSON, so nothing has to be translated.
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
                context_window_tokens=self._config.context_window_tokens,
            ),
            notebook=self._notebook,
        )
