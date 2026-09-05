"""Token-footprint usage analytics for rio SKILL.state coding sessions.

tau's ``session_usage.py`` collected per-request provider-reported usage
(fresh/cached/cache-write/output token counts straight off each
``AssistantMessage.usage``) from an append-only transcript, then rendered an
interactive HTML dashboard from it.

Neither half survives the port unchanged. There is no transcript to walk --
a rio session journal is a sequence of committed
``rio_coding.session_store`` entries, one per SKILL.state step -- and no
provider ever reports real usage for a step: ``rio_agent.loop`` validates the
assistant's reply and discards its reasoning before the loop's caller ever
sees the raw message, so ``AssistantMessage.usage`` never reaches the
journal. What is left to measure is the same fixed three-piece prompt every
step actually sends, so per-step usage here is a
``rio_coding.step_footprint`` estimate rather than a provider-reported
figure.

Presentation lives in the TUI and session export layers. ``SessionUsage``
below provides the estimated accounting data for those renderers.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from rio_ai.tools import AgentTool
from rio_coding.provider_catalog import builtin_provider_entry, model_cost_for_input_tokens
from rio_coding.session_store.entries import (
    BranchSummaryEntry,
    ModelChangeEntry,
    SessionEntry,
    StepEntry,
    ThinkingLevelChangeEntry,
)
from rio_coding.step_footprint import StepFootprint, estimate_step_footprint

__all__ = [
    "SessionUsage",
    "StepUsage",
    "UsageEvent",
    "collect_session_usage",
    "estimated_step_cost",
]

_TOKENS_PER_MILLION = 1_000_000


@dataclass(frozen=True, slots=True)
class StepUsage:
    """Estimated token footprint and chosen action for one committed step."""

    number: int
    timestamp: str
    action_name: str
    instructions_tokens: int
    state_tokens: int
    observation_tokens: int
    tools_tokens: int
    estimated_cost: float | None

    @property
    def total_tokens(self) -> int:
        return (
            self.instructions_tokens
            + self.state_tokens
            + self.observation_tokens
            + self.tools_tokens
        )


@dataclass(frozen=True, slots=True)
class UsageEvent:
    """A notable session event positioned against the step that follows it."""

    step_number: int
    timestamp: str
    kind: str
    label: str


@dataclass(frozen=True, slots=True)
class SessionUsage:
    """Aggregated estimated usage for the steps in a session journal."""

    steps: tuple[StepUsage, ...]
    action_calls: tuple[tuple[str, int], ...]
    events: tuple[UsageEvent, ...] = ()

    @property
    def total_tokens(self) -> int:
        return sum(item.total_tokens for item in self.steps)

    @property
    def total_cost(self) -> float | None:
        costs = [item.estimated_cost for item in self.steps if item.estimated_cost is not None]
        return sum(costs) if costs else None


def estimated_step_cost(
    provider: str,
    model: str,
    footprint: StepFootprint,
) -> float | None:
    """Estimate one step's USD cost from the built-in provider catalog rates.

    The whole footprint is priced at the model's input rate. A SKILL.state
    step has no reused conversation prefix to mark as a cache hit the way a
    growing transcript would -- the state and observation are different on
    every step -- so there is no fresh/cached split to model here.
    """
    entry = builtin_provider_entry(provider)
    metadata = entry.model_metadata.get(model) if entry is not None else None
    if metadata is None:
        return None
    rates = model_cost_for_input_tokens(metadata, footprint.total_tokens)
    if not rates:
        return None
    return footprint.total_tokens * rates.get("input", 0.0) / _TOKENS_PER_MILLION


def collect_session_usage(
    entries: Sequence[SessionEntry],
    *,
    instructions: str,
    tools: Sequence[AgentTool] = (),
    provider: str | None = None,
    model: str | None = None,
) -> SessionUsage:
    """Collect per-step estimated token usage, action counts, and notable events.

    ``instructions`` and ``tools`` are the skill's fixed instructions and the
    per-step tool definitions (normally just ``skill_step_tool(skill)``) --
    the same inputs ``rio_coding.step_footprint.estimate_step_footprint``
    needs, since a step entry only stores the state and observation halves
    of its prompt.
    """
    steps: list[StepUsage] = []
    actions: dict[str, int] = {}
    pending_events: list[tuple[str, str, str]] = []
    events: list[UsageEvent] = []

    for entry in entries:
        event = _usage_event(entry)
        if event is not None:
            kind, label = event
            pending_events.append((_entry_time(entry.timestamp), kind, label))
            continue
        if not isinstance(entry, StepEntry):
            continue

        actions[entry.action.name] = actions.get(entry.action.name, 0) + 1
        footprint = estimate_step_footprint(
            instructions=instructions,
            state=entry.state,
            observation=entry.observation or "",
            tools=tools,
        )
        estimated = (
            estimated_step_cost(provider, model, footprint)
            if provider is not None and model is not None
            else None
        )
        step_number = len(steps) + 1
        steps.append(
            StepUsage(
                number=step_number,
                timestamp=_entry_time(entry.timestamp),
                action_name=entry.action.name,
                instructions_tokens=footprint.instructions_tokens,
                state_tokens=footprint.state_tokens,
                observation_tokens=footprint.observation_tokens,
                tools_tokens=footprint.tools_tokens,
                estimated_cost=estimated,
            )
        )
        events.extend(
            UsageEvent(step_number=step_number, timestamp=timestamp, kind=kind, label=label)
            for timestamp, kind, label in pending_events
        )
        pending_events.clear()

    if steps:
        events.extend(
            UsageEvent(step_number=len(steps), timestamp=timestamp, kind=kind, label=label)
            for timestamp, kind, label in pending_events
        )

    ordered_actions = tuple(sorted(actions.items(), key=lambda item: (-item[1], item[0])))
    return SessionUsage(steps=tuple(steps), action_calls=ordered_actions, events=tuple(events))


def _entry_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).strftime("%H:%M:%S")


def _usage_event(entry: SessionEntry) -> tuple[str, str] | None:
    """Return chart metadata for session events that can affect step footprint."""
    if isinstance(entry, ModelChangeEntry):
        return "model", f"Model changed to {entry.model}"
    if isinstance(entry, ThinkingLevelChangeEntry):
        return "thinking", f"Thinking changed to {entry.thinking_level or 'off'}"
    if isinstance(entry, BranchSummaryEntry):
        return "branch", "Branch summary"
    return None
