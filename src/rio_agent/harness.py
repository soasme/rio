"""SkillStateHarness: a reusable stateful runtime around `run_skill_loop`.

Deliberately mirrors the public shape of `rio_ai`'s ported tau_agent-style
`AgentHarness` -- construct with a config, `subscribe()` an event listener,
call `run()`/`prompt()` to drive it, `cancel()` to stop it -- so callers
already familiar with that loop interface can pick this one up directly.
The difference is entirely internal: instead of holding a growing message
transcript, this harness holds only the current execution state Σ_t and
replays `run_skill_loop`'s bounded per-step prompt.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from inspect import isawaitable

from rio_agent.events import RunEndEvent, SkillEvent, StateUpdateEvent
from rio_agent.loop import run_skill_loop
from rio_agent.skill import SkillSpec
from rio_ai.provider import ModelProvider
from rio_ai.types import JSONObject, JSONValue

EventListener = Callable[[SkillEvent], Awaitable[None] | None]


@dataclass(slots=True)
class SkillStateHarnessConfig:
    provider: ModelProvider
    model: str
    skill: SkillSpec
    max_steps: int | None = None
    max_retries: int = 2


class SimpleCancellationToken:
    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def is_cancelled(self) -> bool:
        return self._cancelled


class SkillStateHarness:
    """Reusable stateful long-horizon agent runtime built on SKILL.state."""

    def __init__(
        self,
        config: SkillStateHarnessConfig,
        *,
        state: JSONObject | None = None,
    ) -> None:
        self._config = config
        self._state: JSONObject = dict(state if state is not None else config.skill.initial_state)
        self._listeners: list[EventListener] = []
        self._current_signal: SimpleCancellationToken | None = None
        self._running = False

    @property
    def state(self) -> dict[str, JSONValue]:
        return dict(self._state)

    @property
    def config(self) -> SkillStateHarnessConfig:
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

    def run(self, observation: str) -> AsyncIterator[SkillEvent]:
        self._ensure_not_running()
        self._running = True
        return self._run(observation)

    async def _run(self, observation: str) -> AsyncIterator[SkillEvent]:
        signal = SimpleCancellationToken()
        self._current_signal = signal
        try:
            async for event in run_skill_loop(
                provider=self._config.provider,
                model=self._config.model,
                skill=self._config.skill,
                observation=observation,
                state=self._state,
                max_steps=self._config.max_steps,
                max_retries=self._config.max_retries,
                signal=signal,
            ):
                if isinstance(event, (StateUpdateEvent, RunEndEvent)):
                    self._state = dict(event.state)
                await self._notify(event)
                yield event
        finally:
            if self._current_signal is signal:
                self._current_signal = None
            self._running = False

    async def _notify(self, event: SkillEvent) -> None:
        for listener in list(self._listeners):
            result = listener(event)
            if isawaitable(result):
                await result

    def _ensure_not_running(self) -> None:
        if self._running:
            raise RuntimeError("SkillStateHarness is already running")
