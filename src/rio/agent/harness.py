"""Harness: a reusable stateful runtime around `run_context_loop`.

Construct with a config, `subscribe()` an event listener, call `run()` to
drive it, `cancel()` to stop it. The harness holds the notebook between runs.
"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from inspect import isawaitable

from rio.agent.events import AgentEvent, RunEndEvent, StepEndEvent, StepStartEvent
from rio.agent.loop import DEFAULT_CONTEXT_WINDOW_TOKENS, run_context_loop
from rio.agent.notebook import Notebook, new_notebook
from rio.agent.spec import HarnessSpec
from rio.ai.provider import ModelProvider

EventListener = Callable[[AgentEvent], Awaitable[None] | None]


@dataclass(slots=True)
class HarnessConfig:
    provider: ModelProvider
    model: str
    skill: HarnessSpec
    max_steps: int | None = None
    max_retries: int = 2
    context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS


class HarnessCancellationToken:
    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def is_cancelled(self) -> bool:
        return self._cancelled


class Harness:
    """Reusable stateful agent runtime whose context is a notebook."""

    def __init__(self, config: HarnessConfig, *, notebook: Notebook | None = None) -> None:
        self._config = config
        self._notebook = copy.deepcopy(notebook) if notebook is not None else new_notebook()
        self._listeners: list[EventListener] = []
        self._current_signal: HarnessCancellationToken | None = None
        self._running = False

    @property
    def notebook(self) -> Notebook:
        return copy.deepcopy(self._notebook)

    @property
    def config(self) -> HarnessConfig:
        return self._config

    @property
    def is_running(self) -> bool:
        return self._running

    def subscribe(self, listener: EventListener) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            with suppress(ValueError):
                self._listeners.remove(listener)

        return unsubscribe

    def cancel(self) -> None:
        if self._current_signal is not None:
            self._current_signal.cancel()

    def run(self, observation: str | None = None) -> AsyncIterator[AgentEvent]:
        """Run once, appending ``observation`` to the notebook as a user cell."""
        if self._running:
            raise RuntimeError("Harness is already running")
        self._running = True
        return self._run(observation)

    async def _run(self, observation: str | None) -> AsyncIterator[AgentEvent]:
        signal = HarnessCancellationToken()
        self._current_signal = signal
        try:
            async for event in run_context_loop(
                provider=self._config.provider,
                model=self._config.model,
                skill=self._config.skill,
                observation=observation,
                notebook=self._notebook,
                max_steps=self._config.max_steps,
                max_retries=self._config.max_retries,
                context_window_tokens=self._config.context_window_tokens,
                signal=signal,
            ):
                if isinstance(event, StepStartEvent | StepEndEvent | RunEndEvent):
                    self._notebook = event.notebook
                await self._notify(event)
                yield event
        finally:
            if self._current_signal is signal:
                self._current_signal = None
            self._running = False

    async def _notify(self, event: AgentEvent) -> None:
        for listener in list(self._listeners):
            result = listener(event)
            if isawaitable(result):
                await result
