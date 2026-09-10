"""rio.agent: a SKILL.state long-horizon agent runtime.

Fixed skill instructions, a structured mutable execution state, and the
latest observation -- an action's result, a user message, or both -- are the
only inputs to each step. The runtime discards the reasoning behind each
step once it commits a valid state update, so per-step prompt size stays
fixed instead of growing with the number of steps already taken.

Exposes a loop interface shaped like `rio.ai`'s ported tau_agent-style
`AgentHarness`/`run_agent_loop` (a stateful harness plus a bare async-
generator loop function, both emitting a typed event stream), but backed
internally by execution state rather than an append-only transcript.
"""

# ruff: noqa: F401 - this module intentionally defines the public facade

from rio.agent.errors import (
    ActionNotFoundError,
    ProviderResponseError,
    RetriesExhaustedError,
    StateValidationError,
)
from rio.agent.events import (
    ActionEndEvent,
    ActionStartEvent,
    ReasoningDiscardedEvent,
    RunEndEvent,
    RunStartEvent,
    SkillEvent,
    StateUpdateEvent,
    StepEndEvent,
    StepStartEvent,
    ValidationErrorEvent,
)
from rio.agent.harness import (
    EventListener,
    Harness,
    HarnessCancellationToken,
    HarnessConfig,
)
from rio.agent.loop import run_skill_loop
from rio.agent.observation import HarnessObservation
from rio.agent.prompt import STEP_TOOL_NAME, build_step_messages, skill_step_tool
from rio.agent.skill import HarnessSpec
from rio.agent.state import (
    apply_state_delta,
    check_state_budget,
    state_size_chars,
    validate_state_delta,
)

__all__ = [name for name in globals() if not name.startswith("_")]
