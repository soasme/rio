"""Harness: a reusable stateful runtime around `run_context_loop`.

Construct with a config, `subscribe()` an event listener, call `run()` to
drive it, `cancel()` to stop it. The harness holds the context between runs
and keeps one context file for its lifetime.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from inspect import isawaitable
from pathlib import Path

from rio.agent.context import Turn
from rio.agent.events import AgentEvent, RunEndEvent, StepEndEvent, StepStartEvent
from rio.agent.loop import DEFAULT_CONTEXT_WINDOW_TOKENS, default_context_file, run_context_loop
from rio.agent.skill import HarnessSpec
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
    context_file: Path | None = None


class HarnessCancellationToken:
    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def is_cancelled(self) -> bool:
        return self._cancelled


class Harness:
    """Reusable stateful agent runtime that manages its own context."""

    def __init__(self, config: HarnessConfig, *, context: list[Turn] | None = None) -> None:
        self._config = config
        self._context: list[Turn] = [dict(item) for item in context or []]
        self._context_file = config.context_file or default_context_file()
        self._listeners: list[EventListener] = []
        self._current_signal: HarnessCancellationToken | None = None
        self._running = False

    @property
    def context(self) -> list[Turn]:
        return [dict(item) for item in self._context]

    @property
    def context_file(self) -> Path:
        return self._context_file

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
        """Run once, appending ``observation`` to the context as a user turn."""
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
                context=self._context,
                context_file=self._context_file,
                max_steps=self._config.max_steps,
                max_retries=self._config.max_retries,
                context_window_tokens=self._config.context_window_tokens,
                signal=signal,
            ):
                if isinstance(event, StepStartEvent | StepEndEvent | RunEndEvent):
                    self._context = [dict(item) for item in event.context]
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
