"""Lifetime activity and estimated token-cost totals for a rio coding session.

Token and cost totals are built on `rio.coding.session_usage`'s per-step
footprint estimates rather than provider-reported usage.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from rio.ai.tools import AgentTool
from rio.coding.session_store.entries import SessionEntry, TurnEntry, ValidationFailureEntry
from rio.coding.session_usage import collect_session_usage

__all__ = ["SessionStats", "calculate_session_stats"]


@dataclass(frozen=True, slots=True)
class SessionStats:
    """Cumulative activity and estimated token cost for one active session branch."""

    turn_count: int = 0
    step_count: int = 0
    validation_error_count: int = 0
    instructions_tokens: int = 0
    context_tokens: int = 0
    tools_tokens: int = 0
    estimated_cost: float | None = None

    @property
    def prompt_tokens(self) -> int:
        """Return cumulative estimated prompt tokens across every step."""
        return self.instructions_tokens + self.context_tokens + self.tools_tokens

    @property
    def average_step_tokens(self) -> float | None:
        """Return the mean estimated prompt size per step."""
        if self.step_count <= 0:
            return None
        return self.prompt_tokens / self.step_count


def calculate_session_stats(
    entries: Sequence[SessionEntry],
    *,
    instructions: str,
    tools: Sequence[AgentTool] = (),
    provider: str | None = None,
    model: str | None = None,
) -> SessionStats:
    """Aggregate estimated token footprint and activity across a session journal.

    One committed step is exactly one action call, so ``step_count`` is
    also the tool-call count.
    """
    turn_count = sum(1 for entry in entries if isinstance(entry, TurnEntry))
    validation_error_count = sum(
        1 for entry in entries if isinstance(entry, ValidationFailureEntry)
    )
    usage = collect_session_usage(
        entries,
        instructions=instructions,
        tools=tools,
        provider=provider,
        model=model,
    )
    return SessionStats(
        turn_count=turn_count,
        step_count=len(usage.steps),
        validation_error_count=validation_error_count,
        instructions_tokens=sum(step.instructions_tokens for step in usage.steps),
        context_tokens=sum(step.context_tokens for step in usage.steps),
        tools_tokens=sum(step.tools_tokens for step in usage.steps),
        estimated_cost=usage.total_cost,
    )
