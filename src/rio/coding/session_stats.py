"""Lifetime activity and estimated token-cost totals for a rio coding session.

tau's ``session_stats.py`` reduced a transcript's ``AssistantMessage.usage``
and ``.timing`` blocks into a single cumulative snapshot for a live status
bar: cache hit rate, output tokens/second, time-to-first-output, and an
estimated dollar cost. None of the timing fields survive this port: a
SKILL.state step's assistant reply is validated and its timing (like its
reasoning) is discarded by ``rio.agent.loop`` before the loop's caller ever
sees it, so there is no data source for response duration or
time-to-first-output any more. The token and cost totals are rebuilt on top
of ``rio.coding.session_usage``'s per-step footprint estimates instead of
provider-reported usage, for the same reason described there.

Also dropped: the ``PricingResolver`` callable indirection tau used to keep
this module decoupled from its own provider catalog. Both accounting modules
in this port already go through ``rio.coding.provider_catalog`` via
``session_usage.estimated_step_cost``, so threading a second pricing
abstraction through here would just be indirection with no caller.
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
    state_tokens: int = 0
    observation_tokens: int = 0
    tools_tokens: int = 0
    estimated_cost: float | None = None

    @property
    def prompt_tokens(self) -> int:
        """Return cumulative estimated prompt tokens across every step."""
        return (
            self.instructions_tokens
            + self.state_tokens
            + self.observation_tokens
            + self.tools_tokens
        )

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

    One committed step is exactly one action call under SKILL.state's
    one-action-per-step rule, so unlike tau's transcript-based stats there is
    no separate tool-call counter to track: ``step_count`` already is that
    count.
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
        state_tokens=sum(step.state_tokens for step in usage.steps),
        observation_tokens=sum(step.observation_tokens for step in usage.steps),
        tools_tokens=sum(step.tools_tokens for step in usage.steps),
        estimated_cost=usage.total_cost,
    )
