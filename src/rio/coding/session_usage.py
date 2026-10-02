"""Token-footprint usage analytics for rio coding sessions.

Per-step usage is a `rio.coding.step_footprint` estimate of each step's prompt,
computed from the context the journal recorded, rather than a provider count.
Presentation lives in the session export layer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from rio.ai.tools import AgentTool
from rio.coding.provider_catalog import builtin_provider_entry, model_cost_for_input_tokens
from rio.coding.session_store.entries import (
    BranchSummaryEntry,
    ModelChangeEntry,
    SessionEntry,
    StepEntry,
    ThinkingLevelChangeEntry,
    entry_context,
)
from rio.coding.step_footprint import StepFootprint, estimate_step_footprint

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
    context_tokens: int
    tools_tokens: int
    estimated_cost: float | None

    @property
    def total_tokens(self) -> int:
        return self.instructions_tokens + self.context_tokens + self.tools_tokens


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

    The whole footprint is priced at the model's input rate; cache hits on an
    unedited context prefix are not modeled.
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

    ``instructions`` and ``tools`` are the skill's fixed instructions and
    action definitions; a step entry only stores its context.
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
            context=entry_context(entry) or [],
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
                context_tokens=footprint.context_tokens,
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
